"""AppState: snapshot vivo del último reporte (reemplaza la `class Snapshot` del
http.server). Async-safe; el refresh loop del lifespan lo actualiza, los routers
lo leen vía Depends(get_state)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Dict, List, Optional

from core.domain.models import InstrumentMetrics


class AppState:
    def __init__(self) -> None:
        self._metrics: List[InstrumentMetrics] = []
        self._by_ticker: Dict[str, InstrumentMetrics] = {}
        self._last_refresh: Optional[datetime] = None
        self._lock = asyncio.Lock()

    async def update(self, metrics: List[InstrumentMetrics]) -> None:
        by_ticker = {
            m.snapshot.instrument.ticker: m
            for m in metrics
            if m.snapshot and m.snapshot.instrument
        }
        async with self._lock:
            self._metrics = metrics
            self._by_ticker = by_ticker
            self._last_refresh = datetime.now()

    def metrics(self) -> List[InstrumentMetrics]:
        return self._metrics

    def by_ticker(self, ticker: str) -> Optional[InstrumentMetrics]:
        return self._by_ticker.get(ticker.upper())

    def price_of(self, ticker: str) -> Optional[float]:
        m = self._by_ticker.get(ticker.upper())
        return m.snapshot.price if m and m.snapshot else None

    @property
    def last_refresh(self) -> Optional[datetime]:
        return self._last_refresh
