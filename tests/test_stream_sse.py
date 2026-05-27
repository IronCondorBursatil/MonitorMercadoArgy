"""SSE (§7.4): notificación de AppState + registro del router /stream."""

import asyncio

from apps.web.app import app
from apps.web.state import AppState


def test_appstate_revision_starts_zero():
    assert AppState().revision == 0


def test_update_increments_revision_and_wakes_waiter():
    async def run():
        st = AppState()
        waiter = asyncio.create_task(st.wait_for_change(0))
        await asyncio.sleep(0)        # cede el control para que el waiter empiece a esperar
        await st.update([])           # incrementa revisión → notify_all
        rev = await asyncio.wait_for(waiter, timeout=1.0)
        assert rev == 1
        assert st.revision == 1
    asyncio.run(run())


def test_wait_for_change_returns_immediately_when_already_changed():
    async def run():
        st = AppState()
        await st.update([])           # revision → 1
        # Un suscriptor que vio last_seen=0 no debe bloquear (ya cambió).
        rev = await asyncio.wait_for(st.wait_for_change(0), timeout=1.0)
        assert rev == 1
    asyncio.run(run())


def test_stream_route_registered():
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/stream" in paths
