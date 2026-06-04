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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from apps.web.deps import get_repo, get_state
from apps.web.routers import (
    abm, bcra, bonds, cartera, cashflows, curva, escenarios, fci, header, options,
    panels, stream,
)
from apps.web.state import AppState
from config.settings import settings
from core.domain.instrument_groups import (
    BOPREALES, CER, DOLAR_LINKED, DUAL_TAMAR, OBLIGACIONES_NEGOCIABLES,
    SOBERANOS, TAMAR, TASA_FIJA,
)
from core.infrastructure.async_http import ResilientClient
from core.infrastructure.provider_hub import ProviderHub

logger = logging.getLogger(__name__)

_ALL_TYPES = [*SOBERANOS, *BOPREALES, *TASA_FIJA, *CER, *DOLAR_LINKED, *TAMAR,
              *DUAL_TAMAR, *OBLIGACIONES_NEGOCIABLES]


async def _refresh_loop(app: FastAPI) -> None:
    """Ingesta async (§6.3-6.5): `hub.refresh_all()` trae Data912 (5 endpoints en
    paralelo, httpx + circuit breaker + pool) y el motor de pricing corre off-loop
    vía `to_thread` leyendo el snapshot ya materializado por el hub. Adicionalmente
    arma la chain enriquecida de opciones (parser + CRR + griegos + tasas)."""
    from core.domain.options.chain import build_options
    from core.infrastructure.provider_hub import HubMarketDataProvider
    from core.use_cases.generate_report import GenerateMonitorReport

    repo = get_repo()
    provider = HubMarketDataProvider(app.state.hub, app.state.provider)
    while True:
        await asyncio.sleep(settings.refresh_sec)
        try:
            await app.state.hub.refresh_all()  # I/O async (no bloquea el event loop)
            use_case = GenerateMonitorReport(repo, provider)
            metrics = await asyncio.to_thread(use_case.execute, _ALL_TYPES)
            await app.state.app_state.update(metrics)
            # Opciones: filtra el snapshot por origen y reconstruye la chain
            # enriquecida fuera del event loop.
            snap = app.state.hub.snapshot()
            sources = app.state.hub.sources()
            opt_rows = {s: r for s, r in snap.items() if sources.get(s) == "options"}
            stk_rows = {s: r for s, r in snap.items() if sources.get(s) == "stocks"}
            items = await asyncio.to_thread(build_options, opt_rows, stk_rows)
            app.state.app_state.set_options(items)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("refresh loop iteration failed")


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


async def _startup_reconcile(app: FastAPI) -> None:
    """Al arranque: trae un snapshot de Data912 y reconcilia el catálogo —
    completa las patas de moneda (MEP/CABLE) de soberanos ya cargados (mismo bono)
    y da de alta las acciones como categoría 'Acciones'. Los tickers de renta fija
    genuinamente nuevos quedan para el alta manual (sidebar del ABM)."""
    try:
        await app.state.hub.refresh_all()
        n = await asyncio.to_thread(_reconcile_catalog, app.state.hub)
        if n:
            get_repo().reload(reseed_from_excel=False)
            logger.info("Startup: catálogo reconciliado (+%d filas: patas + acciones).", n)
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
        get_price_history_store, prime_from_data912, record_live_closes,
    )

    repo = get_repo()
    store = get_price_history_store()
    provider = app.state.provider  # Data912MarketDataProvider (tiene fetch_bond_history)
    primed = False
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
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("price history loop iteration failed")
        await asyncio.sleep(settings.price_history_sec)


async def _bei_loop(app: FastAPI) -> None:
    """Loop dedicado de BEI (pesado: bootstrap + NSS fits). Corre 1× al arranque
    y luego cada bei_refresh_sec. Reemplaza el daemon _bei_refresh_loop."""
    from apps.cli.monitors.bei import compute_bei_tables
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
            use_case = GenerateMonitorReport(repo, provider)
            tables = await asyncio.to_thread(
                compute_bei_tables, use_case=use_case, indices_provider=bcra)
            app.state.app_state.set_bei(tables)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("BEI loop iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from core.infrastructure.cafci_provider import CAFCIProvider
    from core.infrastructure.fx_provider import DolarAPIProvider
    from core.infrastructure.futures_provider import RofexProvider
    from core.infrastructure.indices_provider import BCRAIndicesProvider
    from core.infrastructure.repositories import Data912MarketDataProvider

    app.state.client = ResilientClient()
    app.state.hub = ProviderHub(app.state.client)
    app.state.app_state = AppState()
    repo = get_repo()  # warm: carga SQLite / siembra desde Excel
    # Providers para el popup de detalle (comparten caches class-level con el refresh).
    app.state.provider = Data912MarketDataProvider()
    app.state.indices = BCRAIndicesProvider(excel_repo=repo)
    app.state.fx = DolarAPIProvider()
    app.state.rofex = RofexProvider()  # WS Matba lazy (warmup en el 1er get_quotes)
    app.state.cafci = CAFCIProvider()  # FCI: hidrata de disco / fetch 1×/día
    # En tests (MONITOR_DISABLE_LOOPS=1) NO arrancamos los loops: corren pricing
    # con indices reales en background y contaminan los caches de módulo
    # (p.ej. el avg TAMAR), rompiendo la aislación del test de equivalencia.
    tasks = []
    if not os.environ.get("MONITOR_DISABLE_LOOPS"):
        tasks = [
            asyncio.create_task(_startup_reconcile(app)),
            asyncio.create_task(_refresh_loop(app)),
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
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
app.include_router(panels.router)
app.include_router(bonds.router)
app.include_router(cartera.router)
app.include_router(bcra.router)
app.include_router(cashflows.router)
app.include_router(escenarios.router)
app.include_router(curva.router)
app.include_router(fci.router)
app.include_router(abm.router)
app.include_router(options.router)
app.include_router(header.router)
app.include_router(stream.router)


@app.get("/api/health")
def health(repo=Depends(get_repo), state=Depends(get_state)):
    return {
        "status": "ok",
        "instruments": len(repo.get_all_instruments()),
        "metrics_cached": len(state.metrics()),
        "last_refresh": state.last_refresh.isoformat() if state.last_refresh else None,
    }


@app.get("/api/metrics")
def metrics(state=Depends(get_state)):
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
