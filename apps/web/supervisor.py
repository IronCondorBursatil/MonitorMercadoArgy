"""Supervisión de las tasks de fondo del lifespan.

`asyncio.create_task` es fire-and-forget: si la corrutina termina —por una excepción
que el propio loop no atrapó, por una **cancelación espuria**, o simplemente porque
retornó— la task desaparece y NADIE la reinicia.

Eso es exactamente lo que pasó el 2026-09-01: `_refresh_loop` murió a las 12:45 UTC y
la app siguió sirviendo el MISMO snapshot ~22hs. Los otros cinco loops seguían vivos
(SSE, opciones, BEI, price history, ratings), así que la web respondía normal y la
única señal era `is_stale` en `/api/health`. Peor: la muerte fue **muda** — el
`except asyncio.CancelledError: raise` de cada loop es correcto para el shutdown, pero
ante una cancelación espuria termina la task sin dejar UNA sola línea de log.

`supervise()` cierra las dos puntas:
  1. reinicia el loop pase lo que pase (excepción, cancelación espuria o retorno), y
  2. deja constancia del motivo (log + `on_crash` → `AppState.record_error`).

Shutdown vs. caída se distinguen con el Event `stopping`, que el lifespan setea
**antes** de cancelar las tasks: con `stopping` seteado la cancelación se honra y se
propaga; sin él, se absorbe y el loop vuelve a arrancar.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Cancelaciones espurias seguidas que se absorben antes de rendirse. Salvaguarda
# anti-livelock: si el event loop se está cerrando por un camino que NO pasa por el
# lifespan (y por lo tanto no setea `stopping`), reintentar para siempre colgaría el
# shutdown — cambiaríamos un bug por otro peor.
_MAX_CONSECUTIVE_CANCELS = 5


async def supervise(
    name: str,
    factory: Callable[[], Awaitable[None]],
    *,
    stopping: asyncio.Event,
    on_crash: Optional[Callable[[str, str], Awaitable[None]]] = None,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    healthy_after: float = 60.0,
    max_consecutive_cancels: int = _MAX_CONSECUTIVE_CANCELS,
    _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    _monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Corre `factory()` para siempre, reiniciándolo si termina por cualquier motivo.

    `factory` es un callable que devuelve una corrutina NUEVA en cada llamada (una
    corrutina ya consumida no se puede re-awaitear).

    El backoff es exponencial entre `base_delay` y `max_delay`, pero se **resetea**
    si la corrida duró al menos `healthy_after`: una caída aislada tras horas sanas
    —el caso real del incidente— reintenta enseguida en vez de arrastrar el backoff
    de un arranque fallido.

    `_sleep`/`_monotonic` se inyectan sólo en tests (evitan esperas reales).
    """
    delay = base_delay
    consecutive_cancels = 0

    while not stopping.is_set():
        started = _monotonic()
        spurious_cancel = False
        try:
            await factory()
            reason = "el loop retornó sin excepción"
        except asyncio.CancelledError:
            if stopping.is_set():
                raise                      # shutdown real: honrar la cancelación
            consecutive_cancels += 1
            if consecutive_cancels >= max_consecutive_cancels:
                logger.error(
                    "loop %r: %d cancelaciones seguidas — el event loop debe estar "
                    "cerrando; me rindo.", name, consecutive_cancels)
                raise
            # Cancelación espuria: absorberla. `uncancel()` limpia el pedido de
            # cancelación pendiente para que el `sleep` de abajo no vuelva a morir.
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            spurious_cancel = True
            reason = "CancelledError espuria (no venía del shutdown)"
        except Exception as e:  # noqa: BLE001 — nada puede matar al supervisor
            reason = f"{type(e).__name__}: {e}"
            logger.exception("loop %r cayó con excepción", name)
        if not spurious_cancel:
            consecutive_cancels = 0

        if stopping.is_set():
            break

        # Una corrida larga = el loop estaba sano; la caída fue puntual → reintento ya.
        ran_for = _monotonic() - started
        if ran_for >= healthy_after:
            delay = base_delay

        logger.warning("loop %r terminó (%s) — reiniciando en %.1fs",
                       name, reason, delay)
        if on_crash is not None:
            try:
                await on_crash(name, reason)
            except Exception:  # noqa: BLE001 — reportar no puede tumbar el supervisor
                logger.exception("on_crash de %r falló", name)

        try:
            await _sleep(delay)
        except asyncio.CancelledError:
            if stopping.is_set():
                raise
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()

        delay = min(delay * 2, max_delay) if delay else base_delay
