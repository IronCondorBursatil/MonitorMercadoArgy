import asyncio
import json
import logging
from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from apps.web.deps import get_state
from apps.web.routers.api_v1.market import build_market_json

logger = logging.getLogger(__name__)

router = APIRouter()

_PING_TIMEOUT_S = 15.0

@router.get("/")
async def stream_v1(request: Request, state=Depends(get_state)):
    """SSE endpoint for React SPA. Pushes JSON data instead of a generic refresh trigger."""
    async def event_gen():
        last = state.revision

        # Sync inicial
        # Solo mandamos bonares por ahora (proof-of-concept), en el futuro se
        # enviará un dict consolidado con los paneles activos del usuario.
        initial_data = build_market_json("bonares", state)
        yield {"event": "market_data", "data": json.dumps(initial_data)}

        while True:
            if await request.is_disconnected():
                break
            try:
                last = await asyncio.wait_for(state.wait_for_change(last), timeout=_PING_TIMEOUT_S)
                # Cuando hay refresh, empaquetamos la data fresca a JSON
                # Idealmente esto se hace 1 vez por ciclo globalmente, no por conexión,
                # pero FastAPI es rápido y para el POC sirve.
                fresh_data = build_market_json("bonares", state)
                yield {"event": "market_data", "data": json.dumps(fresh_data)}
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": ""}
            except asyncio.CancelledError:
                break

    return EventSourceResponse(event_gen())
