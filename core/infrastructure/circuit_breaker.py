"""Circuit breaker async (deuda #5). Estados CLOSED → OPEN → HALF_OPEN.

Tras `fail_max` fallos consecutivos el breaker ABRE: durante `reset_timeout`
segundos rechaza llamadas con `CircuitOpenError` (el caller usa cache stale en
vez de spamear reintentos a un upstream caído). Pasado el timeout pasa a
HALF_OPEN y deja pasar UNA prueba: si va bien CIERRA, si falla vuelve a ABRIR.

Uso:
    breaker = CircuitBreaker(name="data912/bonds")
    try:
        async with breaker:
            resp = await client.get(url)
    except CircuitOpenError:
        ...  # usar cache stale
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Optional


class CircuitOpenError(Exception):
    """El breaker está OPEN: la llamada se rechaza sin tocar la red."""


class _State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, fail_max: int = 4, reset_timeout: float = 30.0, name: str = "cb"):
        self.name = name
        self._fail_max = fail_max
        self._reset_timeout = reset_timeout
        self._state = _State.CLOSED
        self._fail_count = 0
        self._opened_at: Optional[float] = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state.value

    async def __aenter__(self) -> "CircuitBreaker":
        async with self._lock:
            if self._state is _State.OPEN:
                assert self._opened_at is not None
                if (time.monotonic() - self._opened_at) >= self._reset_timeout:
                    self._state = _State.HALF_OPEN  # dejar pasar una prueba
                else:
                    raise CircuitOpenError(f"{self.name} OPEN")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        async with self._lock:
            if exc_type is None:
                # éxito → cierra y resetea
                self._fail_count = 0
                self._state = _State.CLOSED
                self._opened_at = None
            elif exc_type is CircuitOpenError:
                pass  # no contar el propio rechazo como fallo
            else:
                self._fail_count += 1
                if self._state is _State.HALF_OPEN or self._fail_count >= self._fail_max:
                    self._state = _State.OPEN
                    self._opened_at = time.monotonic()
        return False  # nunca suprime la excepción original
