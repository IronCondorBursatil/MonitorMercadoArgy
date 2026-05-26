"""App FastAPI (Fase 4 — fundación).

Integra el trabajo de Fases 1-3:
  - CatalogRepository (SQLite, Fase 2) vía Depends(get_repo).
  - Motor financiero refactorizado (Fase 1) vía GenerateMonitorReport.
  - Puente CPU (§6.5): `await asyncio.to_thread(use_case.execute, ...)` corre el
    pricing pesado fuera del event loop.
  - ResilientClient + ProviderHub (Fase 3) en app.state, listos para que los
    providers async los usen.
  - lifespan + asyncio.create_task reemplazan los daemon threads + _SHUTDOWN_EVENT.

Estado: FUNDACIÓN runnable. El refresh loop reusa los providers sync existentes
vía to_thread (la reescritura async de los 6 providers y la migración de TODOS
los paneles/páginas a HTMX/Jinja + retiro del SPA siguen pendientes). `run.py`
sigue apuntando al http.server hasta alcanzar paridad; esta app se levanta con:

    & "$env:LOCALAPPDATA\\Microsoft\\WindowsApps\\python3.12.exe" -m uvicorn apps.web.app:app --port 8001
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
from apps.web.routers import abm, bcra, bonds, cartera, cashflows, panels
from apps.web.state import AppState
from config.settings import settings
from core.domain.instrument_groups import (
    BOPREALES, CER, DOLAR_LINKED, DUAL_TAMAR, SOBERANOS, TAMAR, TASA_FIJA,
)
from core.infrastructure.async_http import ResilientClient
from core.infrastructure.provider_hub import ProviderHub

logger = logging.getLogger(__name__)

_ALL_TYPES = [*SOBERANOS, *BOPREALES, *TASA_FIJA, *CER, *DOLAR_LINKED, *TAMAR, *DUAL_TAMAR]


async def _refresh_loop(app: FastAPI) -> None:
    """Reemplaza _refresh_loop daemon. Reusa GenerateMonitorReport (motor Fase 1)
    + providers sync vía to_thread (puente CPU §6.5)."""
    from core.infrastructure.repositories import Data912MarketDataProvider
    from core.use_cases.generate_report import GenerateMonitorReport

    repo = get_repo()
    while True:
        await asyncio.sleep(settings.refresh_sec)
        try:
            use_case = GenerateMonitorReport(repo, Data912MarketDataProvider())
            metrics = await asyncio.to_thread(use_case.execute, _ALL_TYPES)
            await app.state.app_state.update(metrics)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("refresh loop iteration failed")


async def _bei_loop(app: FastAPI) -> None:
    """Loop dedicado de BEI (pesado: bootstrap + NSS fits). Corre 1× al arranque
    y luego cada bei_refresh_sec. Reemplaza el daemon _bei_refresh_loop."""
    from apps.cli.monitors.bei import compute_bei_tables
    from core.infrastructure.repositories import Data912MarketDataProvider
    from core.use_cases.generate_report import GenerateMonitorReport

    repo = get_repo()
    bcra = app.state.indices
    first = True
    while True:
        if not first:
            await asyncio.sleep(settings.bei_refresh_sec)
        first = False
        try:
            use_case = GenerateMonitorReport(repo, Data912MarketDataProvider())
            tables = await asyncio.to_thread(
                compute_bei_tables, use_case=use_case, indices_provider=bcra)
            app.state.app_state.set_bei(tables)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("BEI loop iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    # En tests (MONITOR_DISABLE_LOOPS=1) NO arrancamos los loops: corren pricing
    # con indices reales en background y contaminan los caches de módulo
    # (p.ej. el avg TAMAR), rompiendo la aislación del test de equivalencia.
    tasks = []
    if not os.environ.get("MONITOR_DISABLE_LOOPS"):
        tasks = [asyncio.create_task(_refresh_loop(app)), asyncio.create_task(_bei_loop(app))]
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
app.include_router(abm.router)


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
