"""ProviderHub: ingesta async coordinada (deuda #6).

`fetch_data912()` trae los 4 endpoints de Data912 en paralelo (asyncio.gather),
valida cada fila con `parse_snapshot_rows` (Pydantic, Fase 1) y mergea SÓLO lo
bueno: si todos fallan, preserva el último snapshot bueno (no wipea el cache) —
misma política que `repositories.Data912MarketDataProvider` pero async y con
breaker por host.

BCRA / DolarAPI / ArgentinaDatos / CAFCI se integran al hub junto con la
reescritura async de esos providers (Fase 4, donde el lifespan de FastAPI corre
el refresh loop nativo en asyncio).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Dict, List

from core.domain.interfaces import IMarketDataProvider
from core.domain.models import MarketSnapshot
from core.infrastructure.async_http import ResilientClient
from core.infrastructure.circuit_breaker import CircuitOpenError
from core.infrastructure.schemas import Data912Row, parse_snapshot_rows

logger = logging.getLogger(__name__)


class ProviderHub:
    DATA912_ENDPOINTS = {
        "notes": "https://data912.com/live/arg_notes",
        "bonds": "https://data912.com/live/arg_bonds",
        "corp": "https://data912.com/live/arg_corp",
        "stocks": "https://data912.com/live/arg_stocks",
        "options": "https://data912.com/live/arg_options",
    }

    def __init__(self, client: ResilientClient):
        self._client = client
        self._snapshot: Dict[str, Data912Row] = {}  # último snapshot bueno (stale-safe)
        self._source: Dict[str, str] = {}           # symbol → endpoint de origen

    async def fetch_data912(self) -> Dict[str, Data912Row]:
        async def _one(name: str, url: str):
            try:
                payload = await self._client.get_json(url, source=f"Data912/{name}")
                return name, parse_snapshot_rows(payload if isinstance(payload, list) else [])
            except CircuitOpenError:
                logger.debug("Data912/%s breaker OPEN; usando stale", name)
                return name, {}
            except asyncio.CancelledError:  # respetar el protocolo de shutdown de asyncio
                raise
            except Exception as e:  # noqa: BLE001 — un endpoint caído no tumba el batch
                logger.warning("Data912/%s fetch failed: %s: %s", name, type(e).__name__, e)
                return name, {}

        results = await asyncio.gather(
            *[_one(n, u) for n, u in self.DATA912_ENDPOINTS.items()]
        )
        merged: Dict[str, Data912Row] = {}
        source: Dict[str, str] = {}
        for name, rows in results:
            for sym in rows:
                source[sym] = name
            merged.update(rows)

        # Preservar stale si todo falló (no wipear → el UI no queda en blanco).
        if merged:
            self._snapshot.update(merged)
            self._source.update(source)
        return dict(self._snapshot)

    def sources(self) -> Dict[str, str]:
        """{symbol: endpoint} del último snapshot (para el listado de faltantes del ABM)."""
        return dict(self._source)

    async def refresh_all(self) -> Dict[str, Data912Row]:
        """Refresca las fuentes async coordinadas. Hoy: Data912 (4 endpoints en
        paralelo, vía ResilientClient). Punto de extensión para FX/BCRA cuando
        esos providers migren a async."""
        return await self.fetch_data912()

    def snapshot(self) -> Dict[str, Data912Row]:
        return dict(self._snapshot)


class HubMarketDataProvider(IMarketDataProvider):
    """`IMarketDataProvider` que sirve los snapshots desde el `ProviderHub` (el
    fetch async ya lo hizo `refresh_all`) y delega el histórico (CSV en disco /
    endpoint de OHLC) a un `Data912MarketDataProvider` sync.

    Esto cablea la ingesta async en el hot-path del refresh loop sin forzar
    async dentro del motor de pricing (que sigue corriendo sync vía to_thread):
    el use-case lee `fetch_snapshots` del snapshot ya materializado por el hub."""

    def __init__(self, hub: ProviderHub, history_provider: object = None):
        self._hub = hub
        if history_provider is None:
            from core.infrastructure.repositories import Data912MarketDataProvider
            history_provider = Data912MarketDataProvider()
        self._hist = history_provider

    def fetch_snapshots(self, tickers: List[str]) -> Dict[str, MarketSnapshot]:
        snap = self._hub.snapshot()
        out: Dict[str, MarketSnapshot] = {}
        today = date.today()
        for ticker in tickers:
            t = str(ticker).upper()
            # _CER es alias de display del lado CER de los duales (TXMJ8_CER → TXMJ8).
            market_t = t[:-4] if t.endswith("_CER") else t
            row = snap.get(market_t) or snap.get(t)
            if not row:
                continue
            out[ticker] = MarketSnapshot(
                instrument=None, price=float(row.c or 0.0), last_update=today,
                bid=row.px_bid, ask=row.px_ask, volume=row.v,
                operations=row.q_op, change_pct=row.pct_change,
            )
        return out

    def fetch_historical_prices(self, ticker: str, days: int):
        return self._hist.fetch_historical_prices(ticker, days)

    def fetch_stock_history(self, ticker: str):
        return self._hist.fetch_stock_history(ticker)
