"""SSE (§7.4): push event-driven del refresh loop a los paneles (vs polling).

`GET /stream` mantiene un canal Server-Sent Events. Cada vez que el refresh loop
actualiza el `AppState`, emite un evento `refresh`; los paneles del dashboard lo
escuchan vía la extensión htmx-sse (`hx-trigger="sse:refresh"`) y recargan su
fragmento exactamente cuando hay datos nuevos. Un `ping` periódico mantiene viva
la conexión a través de proxies.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from apps.web.deps import get_state

logger = logging.getLogger(__name__)

router = APIRouter()

_PING_TIMEOUT_S = 15.0  # keepalive si no hubo refresh en este lapso


@router.get("/stream")
async def stream(request: Request, state=Depends(get_state)):
    async def event_gen():
        last = state.revision
        # Sync inicial: que el cliente arranque alineado al último snapshot.
        yield {"event": "refresh", "data": str(last)}
        while True:
            # NO chequeamos request.is_disconnected() acá: sse-starlette ya corre su
            # propio _listen_for_disconnect sobre el mismo canal ASGI `receive`; dos
            # consumidores del mismo canal es comportamiento indefinido. Al desconectar,
            # sse-starlette cancela este generador → CancelledError abajo.
            try:
                last = await asyncio.wait_for(state.wait_for_change(last), timeout=_PING_TIMEOUT_S)
                yield {"event": "refresh", "data": str(last)}
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": ""}
            except asyncio.CancelledError:  # cliente desconectó
                break

    # send_timeout: si el buffer TCP del cliente se llena, el yield bloquearía para
    # siempre (el generador nunca vuelve a chequear el disconnect y el waiter de la
    # Condition queda colgado). Con timeout, sse-starlette corta la conexión trabada.
    return EventSourceResponse(event_gen(), send_timeout=10)
