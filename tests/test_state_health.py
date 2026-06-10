"""Observabilidad de AppState: staleness + último error (M2.1 / O1).

Si el refresh loop falla, la app seguía sirviendo el último snapshot bueno SIN
ninguna señal: el usuario no podía saber que los datos estaban viejos. Acá se
registra el fallo y se expone el estado de frescura."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from apps.web.state import AppState


def test_fresh_state_after_update_is_not_stale():
    async def run():
        st = AppState()
        await st.update([])
        s = st.status(stale_after_s=30)
        assert s["is_stale"] is False
        assert s["last_error"] is None
        assert s["ok"] is True
        assert s["age_seconds"] is not None and s["age_seconds"] >= 0
    asyncio.run(run())


def test_never_refreshed_is_stale():
    st = AppState()
    s = st.status(stale_after_s=30)
    assert s["is_stale"] is True
    assert s["last_refresh"] is None
    assert s["ok"] is False


def test_age_beyond_threshold_is_stale():
    async def run():
        st = AppState()
        await st.update([])
        future = datetime.now() + timedelta(seconds=120)
        s = st.status(stale_after_s=30, now=future)
        assert s["is_stale"] is True
        assert s["ok"] is False
        assert s["age_seconds"] >= 120
    asyncio.run(run())


def test_record_error_is_observable_and_bumps_revision():
    async def run():
        st = AppState()
        await st.update([])            # revision → 1, ok
        await st.record_error("boom: data912 timeout")
        assert st.revision == 2, "un error debe notificar a los suscriptores SSE"
        s = st.status(stale_after_s=30)
        assert s["last_error"].startswith("boom")
        assert s["last_error_at"] is not None
        assert s["ok"] is False, "con error pendiente, ok=False aunque la data sea reciente"
    asyncio.run(run())


def test_successful_update_clears_previous_error():
    async def run():
        st = AppState()
        await st.record_error("transient blip")
        await st.update([])            # un refresh exitoso limpia el error
        s = st.status(stale_after_s=30)
        assert s["last_error"] is None
        assert s["ok"] is True
    asyncio.run(run())


def test_error_message_is_bounded():
    st = AppState()
    asyncio.run(st.record_error("x" * 5000))
    assert len(st.status(stale_after_s=30)["last_error"]) <= 300
