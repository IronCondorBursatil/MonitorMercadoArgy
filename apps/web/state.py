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
        self._bei: Optional[dict] = None  # tablas crudas de compute_bei_tables
        self._options: list = []          # list[OptionItem] del último refresh (vacío hasta que arme)
        self._options_by_ticker: Dict[str, object] = {}
        self._lock = asyncio.Lock()
        # Notificación para SSE (§7.4): cada update incrementa _revision y
        # despierta a los suscriptores de /stream (push event-driven vs polling).
        self._revision = 0
        self._cond = asyncio.Condition()

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
        await self._notify()

    async def _notify(self) -> None:
        async with self._cond:
            self._revision += 1
            self._cond.notify_all()

    @property
    def revision(self) -> int:
        return self._revision

    async def wait_for_change(self, last_seen: int) -> int:
        """Bloquea hasta que la revisión difiera de `last_seen`; devuelve la nueva."""
        async with self._cond:
            await self._cond.wait_for(lambda: self._revision != last_seen)
            return self._revision

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

    def set_bei(self, tables: Optional[dict]) -> None:
        self._bei = tables  # un solo escritor (el BEI loop); asignación atómica

    def bei_tables(self) -> Optional[dict]:
        return self._bei

    def set_options(self, items: list) -> None:
        """Setea la chain enriquecida de opciones (escrita por el refresh loop)."""
        self._options = items or []
        self._options_by_ticker = {it.ticker: it for it in self._options}

    def options(self) -> list:
        return self._options

    def option(self, ticker: str):
        """Devuelve el OptionItem del ticker dado o None."""
        return self._options_by_ticker.get(ticker.upper())
