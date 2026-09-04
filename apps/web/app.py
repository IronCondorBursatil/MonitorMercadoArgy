"""App FastAPI + HTMX — dashboard del monitor (reemplaza el http.server + SPA).

`run.py` la levanta vía uvicorn (es la app primaria). Integra:
  - CatalogRepository (SQLite) vía Depends(get_repo).
  - Motor financiero (pricing core Strategy/Protocol) vía GenerateMonitorReport.
  - Puente CPU: `await asyncio.to_thread(use_case.execute, ...)` corre el pricing
    pesado fuera del event loop; `_bei_loop` hace lo mismo con compute_bei_tables.
  - lifespan + asyncio.create_task reemplazan los daemon threads + _SHUTDOWN_EVENT
    del http.server (shutdown explícito al cancelar las tasks).
  - ResilientClient + ProviderHub (async) en app.state: la ingesta de la fuente
    live corre async, y FX/indices/REM tienen `prefetch(client)` cableado en el
    refresh loop. Quedan sync CAFCI y argentinadatos (deliberado, ver CLAUDE.md).

Routers en apps/web/routers/, templates Jinja+HTMX en apps/web/templates/.
Bajo pytest (MONITOR_DISABLE_LOOPS=1) los loops no arrancan (aíslan el cache de
módulo del avg TAMAR del test de equivalencia).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from html import escape
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from apps.web.deps_auth import (
    RequireTabPermission, RequiresLoginException, TabForbiddenException,
    get_current_user, get_current_user_html,
)
from apps.web.routers import auth as auth_router, users_abm


from apps.web.deps import get_bondterminal, get_repo, get_state
from apps.web.routers import (
    abm, bcra, bonds, cartera, cashflows, catalog, curva, escenarios, fci, header,
    on, options, panels, source, stream,
)
from apps.web.state import AppState
from apps.web.supervisor import supervise
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


def _catalog_health_report(repo) -> dict:
    """Reporte publicable de la salud del catálogo, leído del repo ya cargado.

    `orphans` = bonos con un `instrument_type` que no pertenece a ningún grupo de
    `instrument_groups`: se cargan, guardan cashflows y acumulan precio, pero NINGÚN
    panel los muestra ni los precia (todo el read-path filtra por igualdad exacta).
    `defaulted` = bonos cuyo tipo se ASUMIÓ del default de una hoja ambigua (una ON
    sin `tipo` se precia como hard-dollar aunque sea dollar-linked: otra moneda de
    pago). `seed_error` = la siembra de bootstrap falló y el catálogo quedó vacío."""
    health = getattr(repo, "type_health", None) or {}
    return {
        "instruments": len(repo.get_all_instruments()),
        "orphans": [e.get("ticker", "") for e in health.get("orphans", ())],
        "defaulted": [e.get("ticker", "") for e in health.get("defaulted", ())],
        "seed_error": getattr(repo, "seed_error", None),
    }


async def _publish_catalog_health(app: FastAPI, repo) -> dict:
    """Cablea la salud del catálogo a `AppState` → `/api/health` (bloque `catalog`)
    y, si la siembra falló, al badge del header vía `record_error`.

    Este es el consumidor que faltaba: `CatalogRepository.type_health` se construyó
    para que el arranque lo publicara, pero sus únicos lectores eran los tests. Una
    señal sin consumidor es exactamente el patrón que dejó el bug original invisible
    durante meses — las filas huérfanas volvían a serlo en silencio.

    El catálogo VACÍO por una semilla ilegible sí es un error de operación (no hay
    nada que servir): va a `record_error` para que el badge lo muestre, y `AppState`
    lo retiene aparte para que el siguiente refresh 'exitoso' de 0 instrumentos no lo
    borre. Los huérfanos NO degradan el semáforo: son crónicos y lo dejarían rojo
    para siempre (ver `AppState.status`)."""
    rep = _catalog_health_report(repo)
    state = app.state.app_state
    state.set_catalog_health(**rep)
    if rep["seed_error"]:
        await state.record_error(
            f"catálogo vacío: la siembra desde el Excel falló ({rep['seed_error']})")
    if rep["orphans"]:
        logger.warning(
            "catálogo: %d bono(s) invisibles en todos los paneles (tipo huérfano) — "
            "publicado en /api/health: %s", len(rep["orphans"]),
            " ".join(rep["orphans"][:20]))
    return rep


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
            # El reload va en su PROPIO try: es la carga del catálogo, no una
            # tarea de enriquecimiento best-effort. Si falla, los paneles siguen
            # sirviendo el cache viejo (sin las patas/ISIN recién escritos) y hasta
            # ahora eso moría en el `except` global de abajo, que sólo loguea —
            # el fallo de carga del repo no llegaba a NINGUNA superficie.
            try:
                get_repo().reload()
            except Exception as e:  # noqa: BLE001
                logger.exception("reload del catálogo falló tras el reconcile")
                await app.state.app_state.record_error(
                    f"catálogo: el reload falló — {type(e).__name__}: {e}")
        # Republicar: el reconcile pudo dar de alta filas (acciones, ONs, patas) y
        # con ellas tipos huérfanos nuevos. Corre igual si el reload falló: entonces
        # el reporte describe el cache que efectivamente se está sirviendo.
        await _publish_catalog_health(app, get_repo())
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
    from datetime import date as _date, timedelta
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
                if primed:
                    # El JSON crudo del priming ya está en el store: soltarlo libera
                    # ~37 MB de RSS que quedaban vivos por un TTL que no le sirve a
                    # nadie (el read-path sale del SQLite, no de este cache).
                    provider.clear_history_cache()
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
        # Poda del store de precios: el read-path solo mira ~400 días, así que todo lo
        # anterior era RAM y disco que nadie leía y que crecía sin techo (~54k filas/año).
        try:
            cutoff = _date.today() - timedelta(days=settings.price_history_keep_days)
            await asyncio.to_thread(store.prune, cutoff)
        except Exception:  # noqa: BLE001 — la poda no debe tumbar el loop
            logger.warning("poda de price_history falló", exc_info=True)
        # Misma higiene para fci_history, que era el único store sin ventana: se carga
        # ENTERO en RAM y entran ~4.700 filas por día de uptime. El read-path usa 12
        # meses. `try` propio: que una poda no se lleve puesta a la otra.
        try:
            corte_fci = _date.today() - timedelta(days=settings.fci_history_keep_days)
            await asyncio.to_thread(fci_store.prune, corte_fci)
        except Exception:  # noqa: BLE001 — la poda no debe tumbar el loop
            logger.warning("poda de fci_history falló", exc_info=True)
        await asyncio.sleep(settings.price_history_sec)


# Tick del monitor de calificaciones. 6h y no 24h a propósito: el corte es idempotente
# por día (`latest_fecha`), así que un tick corto no re-scrapea — lo que compra es
# REINTENTO: si fixscr.com está caído a la hora del arranque, el día todavía tiene
# 3 chances más antes de perderse. No va a settings: no hay nada que tunear en runtime.
_RATINGS_TICK_SEC = 6 * 3600


def _invalidate_ratings_cache() -> None:
    """Suelta el cache del read-path de calificaciones para que el corte recién grabado
    se vea EN CALIENTE (mismo criterio que el `repo.reload()` de la ABM).

    Se prefiere el hook explícito del módulo; si no lo expone, se barren los
    `cache_clear` de sus miembros memoizados (`_entries` arma el merge CSV+store y
    `rating_for` memoiza el matcher por emisor). El barrido genérico evita acoplar este
    loop a QUÉ funciones cachea `ratings.py` — si mañana el cache se rekeya por fecha de
    corte y se invalida solo, esto queda como un no-op inofensivo."""
    from core.infrastructure import ratings

    hook = getattr(ratings, "invalidate_cache", None)
    if callable(hook):
        hook()
        return
    for obj in vars(ratings).values():
        clear = getattr(obj, "cache_clear", None)
        if callable(clear):
            clear()


def _ratings_corte(store, hoy) -> dict:
    """Un corte completo de FIX SCR (SYNC: corre en `to_thread`) — scrape → mejor fila
    por entidad → `record_corte`. Vive fuera del loop para que las ~14 requests al sitio
    y el write SQLite queden en UN solo hop de thread.

    `mejor_fila_por_entidad` es la política del spec (Emisor > Endeudamiento de Largo
    Plazo, sin emisiones `sf(arg)`): sin ella el store vería varias filas por entidad y
    el diff diario marcaría cambios fantasma según cuál ganara ese día."""
    from core.infrastructure import fix_ratings

    mejores = fix_ratings.mejor_fila_por_entidad(fix_ratings.fetch_listado())
    rows = {ent: {"rating": f.rating_lp, "perspectiva": f.perspectiva,
                  "area": f.area, "sector": f.sector}
            for ent, f in mejores.items()}
    return store.record_corte(rows, hoy)


async def _ratings_loop(app: FastAPI) -> None:
    """Monitor diario de calificaciones FIX SCR (spec 2026-08-31): persiste el corte del
    día y deja el diff up/down/watch que el panel ON muestra como badge por 7 días.

    Corre 1× al arranque y luego cada `_RATINGS_TICK_SEC`. El chequeo de `latest_fecha`
    ANTES de scrapear es el que hace restart-safe al proceso: `record_corte` ya es
    idempotente por día, pero preguntarle primero al store ahorra las 14 requests contra
    un sitio que nos deja scrapearlo por cortesía. Todo (red + SQLite) va en `to_thread`:
    el scrape dura decenas de segundos y bloquearía el event loop —y con él el SSE de
    todos los paneles. Un fallo se loguea y se reintenta al tick siguiente: el panel
    sigue sirviendo el último corte bueno, con su `as_of` real a la vista."""
    from datetime import date as _date

    from core.infrastructure.ratings_history import get_ratings_history_store

    store = get_ratings_history_store()
    first = True
    while True:
        if not first:
            await asyncio.sleep(_RATINGS_TICK_SEC)
        first = False
        try:
            hoy = _date.today()
            if await asyncio.to_thread(store.latest_fecha) == hoy.isoformat():
                continue                      # corte del día ya guardado → ni una request
            res = await asyncio.to_thread(_ratings_corte, store, hoy)
            logger.info("FIX ratings: corte %s %s — %d filas, %d cambios%s",
                        res.get("fecha"), res.get("status"), res.get("rows", 0),
                        res.get("changes", 0),
                        f" ({res['reason']})" if res.get("reason") else "")
            if res.get("status") == "ok":
                # Solo un corte REALMENTE grabado cambia el read-path; con noop/discarded
                # /error tirar el cache sería releer el CSV y rearmar el matcher al pedo.
                _invalidate_ratings_cache()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — un scrape caído no puede tumbar el lifespan
            logger.exception("ratings loop iteration failed")


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


def _crash_reporter(app: FastAPI):
    """`on_crash` del supervisor → `AppState.record_loop_crash(name, reason)`.

    Se le pasa el nombre del loop ESTRUCTURADO, no embebido en una frase: antes esto
    armaba "loop {name} cayó ({reason}) — reiniciando" y `AppState` volvía a sacarle
    el nombre con un regex, así que cambiar una palabra del mensaje (o un `reason`
    largo, que la truncación a 300 chars cortaba antes del ')') desviaba la caída al
    canal equivocado EN SILENCIO. Función de módulo —no un closure adentro del
    lifespan— para que el wiring sea testeable sin levantar la app."""
    async def _on_crash(name: str, reason: str) -> None:
        # Que la caída deje rastro (registro por loop + badge si el loop es crítico):
        # el incidente del 2026-09-01 duró 22hs justamente por ser mudo.
        await app.state.app_state.record_loop_crash(name, reason)
    return _on_crash


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
    # Salud del catálogo → AppState (badge + /api/health). Incluye el fallo de la
    # SIEMBRA: `CatalogRepository` ya no lo deja explotar el arranque (mataba también
    # /login y /api/health, o sea la superficie donde se lee el motivo), lo publica.
    await _publish_catalog_health(app, repo)
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
    # `stopping` distingue el shutdown de una caída: el supervisor lo mira para saber
    # si una CancelledError es legítima (apagando) o espuria (hay que reiniciar).
    stopping = asyncio.Event()
    app.state.stopping = stopping
    if not os.environ.get("MONITOR_DISABLE_LOOPS"):
        _on_crash = _crash_reporter(app)
        # `_startup_reconcile` NO se supervisa: corre una vez y terminar es su contrato.
        # Los otros cinco son `while True` — si terminan, es una caída (ver supervisor.py).
        tasks = [asyncio.create_task(_startup_reconcile(app))]
        tasks += [
            asyncio.create_task(
                supervise(name, lambda fn=fn: fn(app), stopping=stopping,
                          on_crash=_on_crash),
                name=f"loop:{name}")
            for name, fn in (
                ("refresh", _refresh_loop),
                ("options", _options_loop),
                ("bei", _bei_loop),
                ("price_history", _price_history_loop),
                ("ratings", _ratings_loop),
            )
        ]
    try:
        yield
    finally:
        stopping.set()   # ANTES de cancelar: le dice al supervisor que no reinicie
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await app.state.client.aclose()


# Docs de OpenAPI APAGADAS por default: FastAPI las monta sobre el router raíz, fuera
# de los `include_router(..., dependencies=[...])` donde vive TODA la auth, y el único
# middleware global es GZip → /openapi.json publicaba el inventario completo de rutas
# (incluida la ABM de usuarios y los nombres de campo de /source/credentials) sin
# cookie. Para levantarlas en desarrollo: MONITOR_ENABLE_DOCS=1 (NUNCA en el droplet:
# las re-expone públicamente, no las pone detrás del login).
_DOCS = bool(os.environ.get("MONITOR_ENABLE_DOCS"))
app = FastAPI(
    title="Monitor Renta Fija AR",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS else None,
    redoc_url="/redoc" if _DOCS else None,
    openapi_url="/openapi.json" if _DOCS else None,
)
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
        # `content` es POSICIONAL y obligatorio en JSONResponse: sin él esto tiraba
        # TypeError y el fragmento HTMX de un usuario deslogueado terminaba en un 500
        # (sin `HX-Redirect`, o sea sin volver al login) en vez de redirigir.
        return JSONResponse({"detail": "login required"}, status_code=200,
                            headers={"HX-Redirect": "/login"})
    return RedirectResponse(url="/login", status_code=302)


@app.exception_handler(TabForbiddenException)
async def tab_forbidden_exception_handler(request: Request, exc: TabForbiddenException):
    """403 'sin permiso' — NUNCA un redirect a /login.

    Falta de PERMISO ≠ falta de LOGIN: el usuario ya se autenticó, mandarlo al
    formulario le dice 'sesión vencida' y lo deja reintentando la clave para siempre.
    Se le muestra qué pestañas SÍ tiene (con link) para que salga de ahí."""
    tabs = {tab: url for tab, url in auth_router._TAB_LANDING}
    links = " · ".join(f'<a href="{escape(url)}">{escape(tab)}</a>'
                       for tab, url in tabs.items() if tab in exc.allowed)
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8"><title>Sin permiso</title>'
        '<div style="font:14px system-ui;max-width:38rem;margin:12vh auto;padding:0 1rem">'
        f'<h1 style="font-size:1.1rem">Sin permiso para «{escape(str(exc.tab))}»</h1>'
        '<p>Tu usuario no tiene habilitada esta pestaña. No es un problema de sesión: '
        'seguís logueado.</p>'
        + (f'<p>Podés ir a: {links}</p>' if links
           else '<p>No tenés ningún módulo habilitado — pedile acceso al administrador.</p>')
        + '<p><a href="/logout">Cerrar sesión</a></p></div>',
        status_code=403)


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



@app.get("/api/health")
def health(repo=Depends(get_repo), state=Depends(get_state)):
    # Público (probes externos): NO expone `last_error` — es el string crudo de una
    # excepción del refresh loop (URLs/params internos de los providers). El detalle
    # del error lo ve el badge del header (/health/badge), que está detrás de login.
    st = state.status()
    return {
        # `status` habla de los PRECIOS (el refresh loop). La caída de un loop
        # lateral (ratings/bei/price_history/options) NO lo degrada —eso sería
        # gritar 'sin datos' con el snapshot fresco de hace 5s— pero se reporta
        # aparte en `degraded_loops` para que ops la vea. Sólo NOMBRES: el motivo
        # es el string crudo de una excepción y este endpoint es público.
        "status": "ok" if st["ok"] else "degraded",
        "instruments": len(repo.get_all_instruments()),
        "metrics_cached": len(state.metrics()),
        "is_stale": st["is_stale"],
        "age_seconds": st["age_seconds"],
        "last_refresh": st["last_refresh"],
        "degraded_loops": st["degraded_loops"],
        # Cuenta de caidas de loops en 24hs. `degraded_loops` sólo dura la ventana de
        # retención (300s) y un probe externo que poléa cada 5 min la pierde entre
        # polls; el contador de 24hs sobrevive a esa granularidad, que es de lo único
        # de lo que un monitor sin infraestructura puede colgar una alerta. Va la
        # CUENTA, no la lista: cada entrada lleva el motivo crudo de la excepción
        # (URLs y params de los providers) y este endpoint es público.
        "loop_crashes_24h": len(st["loop_crashes"]),
        # Salud del CATÁLOGO: cuántos bonos quedaron invisibles (tipo huérfano),
        # cuántos tienen el tipo ASUMIDO por un default ambiguo y si la siembra de
        # bootstrap falló. Sólo CUENTAS y un booleano — el motivo crudo del fallo
        # (paths del servidor) y el inventario de tickers se quedan del lado privado.
        "catalog": st["catalog"],
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
