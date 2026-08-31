"""App FastAPI + HTMX — dashboard del monitor (reemplaza el http.server + SPA).

`run.py` la levanta vía uvicorn (es la app primaria). Integra:
  - CatalogRepository (SQLite) vía Depends(get_repo).
  - Motor financiero (pricing core Strategy/Protocol) vía GenerateMonitorReport.
  - Puente CPU: `await asyncio.to_thread(use_case.execute, ...)` corre el pricing
    pesado fuera del event loop; `_bei_loop` hace lo mismo con compute_bei_tables.
  - lifespan + asyncio.create_task reemplazan los daemon threads + _SHUTDOWN_EVENT
    del http.server (shutdown explícito al cancelar las tasks).
  - ResilientClient + ProviderHub (async) en app.state, listos para cuando los
    providers migren a async (hoy corren sync vía to_thread).

Routers en apps/web/routers/, templates Jinja+HTMX en apps/web/templates/.
Bajo pytest (MONITOR_DISABLE_LOOPS=1) los loops no arrancan (aíslan el cache de
módulo del avg TAMAR del test de equivalencia).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from apps.web.deps_auth import RequiresLoginException, get_current_user_html, get_current_user, RequireTabPermission
from apps.web.routers import auth as auth_router, users_abm


from apps.web.deps import get_bondterminal, get_repo, get_state
from apps.web.routers import (
    abm, bcra, bonds, cartera, cashflows, catalog, curva, escenarios, fci, header,
    on, options, panels, source, stream,
)
from apps.web.routers.api_v1 import market as api_market
from apps.web.routers.api_v1 import stream as api_stream
from apps.web.state import AppState
from config.settings import settings
from core.domain.instrument_groups import (
    BOPREALES, CER, DOLAR_LINKED, DUAL_TAMAR, OBLIGACIONES_NEGOCIABLES,
    PROVINCIALES, SOBERANOS, TAMAR, TASA_FIJA,
)
from core.infrastructure.async_http import ResilientClient
from core.infrastructure.provider_hub import ProviderHub

logger = logging.getLogger(__name__)

_ALL_TYPES = [*SOBERANOS, *BOPREALES, *TASA_FIJA, *CER, *DOLAR_LINKED, *TAMAR,
              *DUAL_TAMAR, *OBLIGACIONES_NEGOCIABLES, *PROVINCIALES]



async def _refresh_loop(app: FastAPI) -> None:
    """Ingesta async (§6.3-6.5): `hub.refresh_all()` trae Data912 (5 endpoints en
    paralelo, httpx + circuit breaker + pool) y el motor de pricing corre off-loop
    vía `to_thread` leyendo el snapshot ya materializado por el hub.

    La chain de opciones (parser + CRR + griegos, ~5-20s) NO va acá: vive en su
    propio `_options_loop` para no espaciar el push SSE de los paneles de bonos —
    el pricing es ~0.1-0.3s, las opciones dominaban el ciclo y lo llevaban a ~25s."""
    from core.infrastructure.provider_hub import HubMarketDataProvider
    from core.use_cases.generate_report import GenerateMonitorReport

    repo = get_repo()
    provider = HubMarketDataProvider(app.state.hub, app.state.provider)
    while True:
        await asyncio.sleep(settings.refresh_sec)
        try:
            _t0 = time.perf_counter()
            await app.state.hub.refresh_all()  # fuente live activa (BYMA/Data912), async
            if hasattr(app.state.indices, "prefetch"):
                await app.state.indices.prefetch(app.state.client)
            if hasattr(app.state.fx, "prefetch"):
                await app.state.fx.prefetch(app.state.client)
            _t_ingest = time.perf_counter()
            use_case = GenerateMonitorReport(repo, provider,
                                             indices=app.state.indices, fx=app.state.fx)
            metrics = await asyncio.to_thread(use_case.execute, _ALL_TYPES)
            await app.state.app_state.update(metrics)   # dispara el SSE `refresh`
            _total = time.perf_counter() - _t0
            # Observabilidad del tiempo de ciclo: si un ciclo supera el intervalo de
            # refresh, los `refresh` del SSE se espacian (los paneles dejan de sentirse
            # "en vivo"). Se grita a WARNING para que quede en el log durable; en
            # operación normal (ciclo < intervalo) va a INFO.
            _lvl = logging.WARNING if _total > settings.refresh_sec else logging.INFO
            logger.log(
                _lvl,
                "refresh cycle: ingest=%.2fs price=%.2fs total=%.2fs (%d instr)",
                _t_ingest - _t0, time.perf_counter() - _t_ingest, _total, len(metrics),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("refresh loop iteration failed")
            # Observabilidad (O1): registrar el fallo para que el header lo muestre.
            # La app sigue sirviendo el último snapshot bueno (stale), pero visible.
            await app.state.app_state.record_error(f"{type(e).__name__}: {e}")


async def _options_loop(app: FastAPI) -> None:
    """Loop dedicado de la chain de opciones (pesado: parser + CRR + griegos de
    ~1000 contratos, ~5-20s). Separado del `_refresh_loop` para no arrastrar el
    push SSE de los paneles de bonos. Corre 1× al arranque y luego cada
    `options_refresh_sec` (default 60s: los griegos no cambian material/segundo)."""
    from core.domain.options.chain import build_options

    first = True
    while True:
        if not first:
            await asyncio.sleep(settings.options_refresh_sec)
        first = False
        try:
            _t0 = time.perf_counter()
            # Snapshot aparte (BYMA open /options por defecto — OI real +
            # underlyingSymbol/optionType/maturityDate autoritativos; Data912 de
            # fallback). El hub elige la fuente y resuelve los subyacentes.
            opt_rows, stk_rows = await app.state.hub.fetch_options(settings.options_source)
            items = await asyncio.to_thread(build_options, opt_rows, stk_rows)
            app.state.app_state.set_options(items)
            _lvl = logging.WARNING if (time.perf_counter() - _t0) > settings.refresh_sec else logging.INFO
            logger.log(_lvl, "options cycle: %.2fs (%d opts)",
                       time.perf_counter() - _t0, len(items))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — un fallo de opciones no debe tumbar el loop
            logger.exception("options loop iteration failed")


def _ensure_obligaciones_negociables() -> int:
    """Bootstrap de las ONs desde el CSV **sólo si la hoja está vacía**.

    Modelo de catálogo (decisión 2026-05-30): la **fuente de verdad en runtime es
    SQLite** (`catalog.db`); el Excel master y `obligaciones_negociables.csv` son
    semillas de *bootstrap* y la ABM es el editor. Por eso el seed NO re-ingesta de
    forma destructiva sobre una hoja ya poblada — eso pisaría las ON cargadas/editadas
    por la ABM (que no están en el CSV, ej. bancos 30/360). Para aplicar cambios del
    CSV a una DB ya poblada, usar una migración explícita o la ABM, no este auto-seed.
    """
    from sqlalchemy import select
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM
    from core.infrastructure.on_catalog import SHEET, ingest

    with SessionLocal() as s:
        ons = s.execute(select(InstrumentORM).where(InstrumentORM.sheet == SHEET)).scalars().all()
    if ons:  # la DB ya tiene ON = la verdad → no re-sembrar (no pisar la ABM)
        return 0
    return ingest()  # hoja vacía → bootstrap inicial desde el CSV


def _reconcile_catalog(hub) -> int:
    """Sync (corre en to_thread): completa patas de moneda de soberanos + da de
    alta las acciones (solo-ticker, categoría Acciones) + carga las ONs hard-dollar.
    Devuelve cuántas filas se agregaron/modificaron."""
    from apps.web.instruments_abm import backfill_soberano_ccy_legs, register_stocks
    from core.domain.instrument_groups import PANEL_LIDER

    snapshot, sources = hub.snapshot(), hub.sources()
    legs = backfill_soberano_ccy_legs(set(snapshot.keys()))
    stock_syms = [s for s, src in sources.items() if src == "stocks"] + list(PANEL_LIDER)
    stocks = register_stocks(stock_syms)
    ons = 0
    try:
        ons = _ensure_obligaciones_negociables()
    except Exception:
        logger.exception("ON ingest failed")
    return len(legs) + len(stocks) + ons


def _backfill_legs() -> int:
    """Completa patas cotizantes faltantes (soberanos + ON) por grupo/ISIN del universo
    BYMA (sync, corre en to_thread). Devuelve cuántas patas se agregaron."""
    from apps.web.instruments_abm import backfill_legs_from_universe
    try:
        res = backfill_legs_from_universe()
        return sum(len(r.get("added", [])) for r in res)
    except Exception:
        logger.exception("backfill de patas (universo) falló")
        return 0


async def _startup_reconcile(app: FastAPI) -> None:
    """Al arranque: trae un snapshot de Data912 y reconcilia el catálogo —
    completa las patas de moneda (MEP/CABLE) de soberanos ya cargados (mismo bono)
    y da de alta las acciones como categoría 'Acciones'. Los tickers de renta fija
    genuinamente nuevos quedan para el alta manual (sidebar del ABM)."""
    from core.infrastructure.byma.catalog_enrich import (
        enrich_ficha_meta, enrich_isin_from_byma, enrich_isin_from_ficha,
    )
    from core.infrastructure.byma.universe import ingest_byma_catalog

    try:
        await app.state.hub.refresh_all()
        n = await asyncio.to_thread(_reconcile_catalog, app.state.hub)
        # ISIN + metadata BYMA (emisor/tipo): primero del seed (instantáneo), luego
        # ficha en vivo para los que quedaron sin ISIN (autoritativo, AL30/DICP/etc).
        enriched = await asyncio.to_thread(enrich_isin_from_byma)
        enriched += await asyncio.to_thread(enrich_isin_from_ficha)
        # Campos ricos de la ficha (ley/moneda/amortización/interés/montos) → para
        # el ABM y el catálogo de productos. Idempotente, best-effort.
        await asyncio.to_thread(enrich_ficha_meta)
        # Universo BYMA navegable (tabla byma_catalog) para el buscador del ABM.
        universe = await asyncio.to_thread(ingest_byma_catalog)
        # Completar patas cotizantes faltantes (soberanos + ON) deduciendo el grupo
        # por el universo BYMA (mismo ISIN). Idempotente. Requiere byma_catalog cargado.
        legs = await asyncio.to_thread(_backfill_legs)
        if n or enriched or legs:
            get_repo().reload()
        logger.info("Startup: catálogo +%d filas, %d ISIN, %d especies BYMA, +%d patas.",
                    n, enriched, universe, legs)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("startup reconcile failed")


async def _price_history_loop(app: FastAPI) -> None:
    """Mantiene el store de cierres diarios (price_history.py) que alimenta las
    variaciones Sem/1M/3M/YTD/1A de los paneles. Cada iteración acumula el cierre
    del feed vivo de TODO el universo; en la 1ª corrida además hace el priming
    profundo de Data912 `/historical/bonds` (soberanos + CER viejos). Read-path
    100% local → esta task es la única que escribe el store. Diario alcanza."""
    from datetime import date as _date
    from core.infrastructure.price_history import (
        byma_prime_candidates, get_price_history_store, prime_from_byma_historico,
        prime_from_data912, record_live_closes,
    )
    from core.infrastructure.fci_history import get_fci_history_store, record_from_ard

    repo = get_repo()
    store = get_price_history_store()
    fci_store = get_fci_history_store()
    provider = app.state.provider  # Data912MarketDataProvider (tiene fetch_bond_history)
    primed = False
    byma_primed = not settings.byma_history_enabled
    byma_attempted: set[str] = set()   # tickers ya intentados de BYMA (1× por proceso)
    while True:
        try:
            # El snapshot lo mantiene fresco `_refresh_loop` (cada 5s) + el reconcile
            # de arranque; leerlo acá evita un refresh_all redundante. Acotamos a los
            # tickers del catálogo (lo que mostramos) → no inflar el store con
            # corp/opciones/stocks ajenos. get_all_instruments ya viene expandido a
            # una especie por ticker (patas ARS/MEP/CABLE incluidas).
            wanted = {i.ticker for i in repo.get_all_instruments()}
            snap = {s: r for s, r in app.state.hub.snapshot().items() if s in wanted}
            n = await asyncio.to_thread(record_live_closes, snap, store, _date.today())
            if not primed:
                got = await asyncio.to_thread(prime_from_data912, list(wanted), provider, store)
                logger.info(
                    "Price history: +%d cierres acumulados, +%d del histórico Data912.",
                    n, got)
                # Solo lo damos por hecho si trajo algo: si Data912 /historical estaba
                # caído (got=0, sin excepción — es best-effort), reintentamos en el
                # próximo tick en vez de quedarnos sin la historia profunda.
                primed = got > 0
            # Completar lo que Data912 no cubre (bopreales, letras, ON, patas MEP/CABLE)
            # con las series históricas de BYMA open. El bloque Data912 de arriba ya
            # corrió este tick, así que los tickers que siguen casi sin historia en el
            # store son justamente los no cubiertos (si Data912 está caído, BYMA cubre
            # todo — degradado pero correcto). Se intenta cada ticker 1× (byma_attempted):
            # evita re-primar en bucle los que BYMA no tiene y no cuelga los que fallan
            # en el mismo lote que un éxito. Listo cuando no queda nada sin intentar.
            if not byma_primed:
                pending = byma_prime_candidates(
                    wanted, store, byma_attempted, settings.byma_history_min_days)
                if pending:
                    byma_attempted.update(pending)
                    gotb = await asyncio.to_thread(
                        prime_from_byma_historico, pending, store,
                        max_days=settings.byma_history_max_days,
                        max_workers=settings.byma_history_workers)
                    logger.info(
                        "Price history BYMA: +%d cierres de %d tickers sin cubrir por Data912.",
                        gotb, len(pending))
                else:
                    byma_primed = True   # nada pendiente sin intentar → listo
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("price history loop iteration failed")
        # FCI: acumula el corte diario de ArgentinaDatos (vcp/ccp/patrimonio) para
        # derivar flujos reales (Δccp×VCP). Independiente del price history (try aparte).
        try:
            nf = await asyncio.to_thread(record_from_ard, fci_store, _date.today())
            if nf:
                logger.info("FCI history: +%d cortes de fondo acumulados.", nf)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("fci history accumulation failed")
        # Backup periódico 1×/día: captura el estado aunque el server lleve días sin
        # reiniciarse (el backup del lifespan solo corre al arranque).
        try:
            from core.infrastructure.db.backup import backup_db
            bak = await asyncio.to_thread(backup_db, settings.catalog_db,
                                          settings.backup_dir, keep=settings.backup_keep)
            if bak:
                logger.info("catalog backup periódico: %s", bak.name)
        except Exception:  # noqa: BLE001 — el backup no debe tumbar el loop
            logger.warning("backup periódico de catalog.db falló", exc_info=True)
        await asyncio.sleep(settings.price_history_sec)


async def _bei_loop(app: FastAPI) -> None:
    """Loop dedicado de BEI (pesado: bootstrap + NSS fits). Corre 1× al arranque
    y luego cada bei_refresh_sec. Reemplaza el daemon _bei_refresh_loop."""
    from apps.cli.bei import compute_bei_tables
    from core.infrastructure.provider_hub import HubMarketDataProvider
    from core.use_cases.generate_report import GenerateMonitorReport

    repo = get_repo()
    bcra = app.state.indices
    provider = HubMarketDataProvider(app.state.hub, app.state.provider)
    first = True
    while True:
        if not first:
            await asyncio.sleep(settings.bei_refresh_sec)
        first = False
        try:
            await app.state.hub.refresh_all()  # snapshot fresco (la 1ª corrida es en startup)
            if hasattr(app.state.indices, "prefetch"):
                await app.state.indices.prefetch(app.state.client)
            if hasattr(app.state.fx, "prefetch"):
                await app.state.fx.prefetch(app.state.client)
            use_case = GenerateMonitorReport(repo, provider,
                                             indices=app.state.indices, fx=app.state.fx)
            tables = await asyncio.to_thread(
                compute_bei_tables, use_case=use_case, indices_provider=bcra)
            app.state.app_state.set_bei(tables)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("BEI loop iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from core.infrastructure.bondterminal_provider import BondTerminalProvider
    from core.infrastructure.cafci_provider import CAFCIProvider
    from core.infrastructure.fx_provider import DolarAPIProvider
    from core.infrastructure.futures_provider import RofexProvider
    from core.infrastructure.indices_provider import BCRAIndicesProvider
    from core.infrastructure.data912_provider import Data912MarketDataProvider

    from core.infrastructure.byma.sources import make_source, Data912Source

    app.state.client = ResilientClient()
    # Fuente live inicial: settings.market_source (default byma_open). Si falla
    # (p.ej. byma_realtime sin credenciales), cae a byma_open y luego a data912.
    try:
        initial_source = make_source(settings.market_source)
    except Exception as e:  # noqa: BLE001
        logger.warning("market source %s no disponible (%s); usando byma_open.",
                       settings.market_source, e)
        try:
            initial_source = make_source("byma_open")
        except Exception:  # noqa: BLE001
            initial_source = Data912Source()
    app.state.hub = ProviderHub(app.state.client, active_source=initial_source)
    app.state.app_state = AppState()
    app.state.app_state.set_data_source(app.state.hub.active_mode,
                                        app.state.hub.active_label,
                                        app.state.hub.is_delayed)
    # Clock congelado por accidente: un MONITOR_AS_OF olvidado en .env congelaría
    # TODOS los precios a una fecha vieja sin señal visible — gritarlo al boot.
    from core.domain.clock import warn_if_frozen
    warn_if_frozen()
    # Backup del catálogo (fuente de verdad viva) ANTES de warmear el repo / migrar:
    # snapshot consistente del estado previo, best-effort (jamás bloquea el arranque).
    try:
        from core.infrastructure.db.backup import backup_db
        bak = await asyncio.to_thread(backup_db, settings.catalog_db, settings.backup_dir,
                                      keep=settings.backup_keep)
        if bak:
            logger.info("catalog backup: %s", bak.name)
    except Exception:  # noqa: BLE001
        logger.warning("backup de catalog.db falló (no bloquea el arranque)", exc_info=True)
    repo = get_repo()  # warm: carga SQLite / siembra desde Excel
    # Providers para el popup de detalle (comparten caches class-level con el refresh).
    app.state.provider = Data912MarketDataProvider()
    app.state.indices = BCRAIndicesProvider(excel_repo=repo)
    app.state.fx = DolarAPIProvider()
    app.state.rofex = RofexProvider()  # WS Matba lazy (warmup en el 1er get_quotes)
    app.state.cafci = CAFCIProvider()  # FCI: hidrata de disco / fetch 1×/día
    app.state.bondterminal = BondTerminalProvider()  # riesgo país EMBI AR (TTL 5min)
    # En tests (MONITOR_DISABLE_LOOPS=1) NO arrancamos los loops: corren pricing
    # con indices reales en background y contaminan los caches de módulo
    # (p.ej. el avg TAMAR), rompiendo la aislación del test de equivalencia.
    tasks = []
    if not os.environ.get("MONITOR_DISABLE_LOOPS"):
        tasks = [
            asyncio.create_task(_startup_reconcile(app)),
            asyncio.create_task(_refresh_loop(app)),
            asyncio.create_task(_options_loop(app)),
            asyncio.create_task(_bei_loop(app)),
            asyncio.create_task(_price_history_loop(app)),
        ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await app.state.client.aclose()


app = FastAPI(title="Monitor Renta Fija AR", lifespan=lifespan)
# GZip: el dataset de /fci/data es grande (~varios MB en JSON) → comprime ~6-7×.
# compresslevel=6 (default de Starlette = 9): mismo tamaño de salida en la práctica,
# ~mitad de CPU por request (medido sobre 4 MB: 107ms→43ms) — para TODA la app.
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)


# ── Estáticos con Cache-Control ─────────────────────────────────────────────
# Sin esto el navegador revalida ~556KB de vendor (chart/gridstack/htmx/html2canvas)
# en CADA navegación de página completa → un round-trip por asset contra el droplet
# = cambio de pestañas lento. Política:
#   • `?v=<hash/mtime>` presente (cache-busting) → immutable 1 año (la URL cambia si
#     cambia el contenido, así que cachear para siempre es seguro y un deploy se ve).
#   • `/vendor/**` (libs de terceros, no cambian entre deploys) → immutable 1 año.
#   • resto (CSS/JS propio sin versionar) → cache corto revalidable: un deploy se
#     ve enseguida, pero una ráfaga de navegación no revalida en cada clic.
_YEAR = "public, max-age=31536000, immutable"
_SHORT = "public, max-age=300"


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            qs = scope.get("query_string", b"")
            versioned = b"v=" in qs
            is_vendor = path.startswith("vendor/") or path.startswith("vendor\\")
            response.headers["Cache-Control"] = _YEAR if (versioned or is_vendor) else _SHORT
        return response


app.mount("/static", CachedStaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")

@app.exception_handler(RequiresLoginException)
async def requires_login_exception_handler(request: Request, exc: RequiresLoginException):
    if request.headers.get("HX-Request"):
        return JSONResponse(status_code=200, headers={"HX-Redirect": "/login"})
    return RedirectResponse(url="/login", status_code=302)


# Servir la app de React en producción bajo /react (mismo cache que los vendor).
react_build_dir = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if react_build_dir.exists():
    app.mount("/react", CachedStaticFiles(directory=str(react_build_dir), html=True), name="react")

app.include_router(auth_router.router)
app.include_router(users_abm.router)

html_deps = [Depends(get_current_user_html)]
api_deps = [Depends(get_current_user)]

app.include_router(panels.router, dependencies=[Depends(RequireTabPermission("bonos"))])
app.include_router(bonds.router, dependencies=[Depends(RequireTabPermission("bonos"))])
app.include_router(on.router, dependencies=[Depends(RequireTabPermission("on"))])
app.include_router(curva.router, dependencies=[Depends(RequireTabPermission("curva"))])
app.include_router(cartera.router, dependencies=[Depends(RequireTabPermission("cartera"))])
app.include_router(bcra.router, dependencies=[Depends(RequireTabPermission("bcra"))])
app.include_router(cashflows.router, dependencies=[Depends(RequireTabPermission("cashflows"))])
app.include_router(fci.router, dependencies=[Depends(RequireTabPermission("fci"))])
app.include_router(escenarios.router, dependencies=[Depends(RequireTabPermission("escenarios"))])
app.include_router(options.router, dependencies=[Depends(RequireTabPermission("opciones"))])
app.include_router(catalog.router, dependencies=[Depends(RequireTabPermission("catalogo"))])
app.include_router(abm.router, dependencies=[Depends(RequireTabPermission("abm"))])

# Parciales globales de HTMX
app.include_router(header.router, dependencies=html_deps)
app.include_router(source.router, dependencies=html_deps)
app.include_router(stream.router, dependencies=html_deps)

# API
app.include_router(api_market.router, prefix="/api/v1/market", dependencies=api_deps)
app.include_router(api_stream.router, prefix="/api/v1/stream", dependencies=api_deps)



@app.get("/api/health")
def health(repo=Depends(get_repo), state=Depends(get_state)):
    # Público (probes externos): NO expone `last_error` — es el string crudo de una
    # excepción del refresh loop (URLs/params internos de los providers). El detalle
    # del error lo ve el badge del header (/health/badge), que está detrás de login.
    st = state.status()
    return {
        "status": "ok" if st["ok"] else "degraded",
        "instruments": len(repo.get_all_instruments()),
        "metrics_cached": len(state.metrics()),
        "is_stale": st["is_stale"],
        "age_seconds": st["age_seconds"],
        "last_refresh": st["last_refresh"],
        "ok": st["ok"],
    }


@app.get("/api/riesgo-pais")
def api_riesgo_pais(bt=Depends(get_bondterminal), _user=Depends(get_current_user)):
    """Riesgo país de BondTerminal: spread ponderado EMBI AR + valor Ambito, deltas, bonos.

    Endpoint de inspección (hermano de /api/health y /api/metrics): expone el payload
    COMPLETO — por-bono, sparkline, calidad del dato — que la card del header no muestra.
    La card se sirve del provider directo (`routers/header.py`), no de esta ruta."""
    data = bt.get_riesgo_pais()
    if data is None:
        return JSONResponse({"error": "no data available"}, status_code=503)
    return JSONResponse(data)


@app.get("/api/metrics")
def metrics(state=Depends(get_state), _user=Depends(get_current_user)):
    """Snapshot JSON del último refresh (prueba el wiring end-to-end del motor)."""
    out = []
    for m in state.metrics():
        inst = m.snapshot.instrument if m.snapshot else None
        out.append({
            "ticker": inst.ticker if inst else None,
            "type": inst.instrument_type if inst else None,
            "price": m.snapshot.price if m.snapshot else None,
            "tir": m.tir,
            "md": m.duration,
            "vtec": m.technical_value,
            "parity": m.parity,
        })
    return JSONResponse(out)
