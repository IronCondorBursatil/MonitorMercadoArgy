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
from typing import Dict

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
    }

    def __init__(self, client: ResilientClient):
        self._client = client
        self._snapshot: Dict[str, Data912Row] = {}  # último snapshot bueno (stale-safe)

    async def fetch_data912(self) -> Dict[str, Data912Row]:
        async def _one(name: str, url: str) -> Dict[str, Data912Row]:
            try:
                payload = await self._client.get_json(url, source=f"Data912/{name}")
                return parse_snapshot_rows(payload if isinstance(payload, list) else [])
            except CircuitOpenError:
                logger.debug("Data912/%s breaker OPEN; usando stale", name)
                return {}
            except Exception as e:  # noqa: BLE001 — un endpoint caído no tumba el batch
                logger.warning("Data912/%s fetch failed: %s: %s", name, type(e).__name__, e)
                return {}

        results = await asyncio.gather(
            *[_one(n, u) for n, u in self.DATA912_ENDPOINTS.items()]
        )
        merged: Dict[str, Data912Row] = {}
        for r in results:
            merged.update(r)

        # Preservar stale si todo falló (no wipear → el UI no queda en blanco).
        if merged:
            self._snapshot.update(merged)
        return dict(self._snapshot)

    def snapshot(self) -> Dict[str, Data912Row]:
        return dict(self._snapshot)
