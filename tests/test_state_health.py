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


def test_record_error_is_observable_without_sse_notify():
    """F2 (review): record_error NO debe bumpear revision — cada fallo de ciclo
    despertaba a todos los paneles SSE para re-fetchear data sin cambios (~12K
    requests/min en outage con K paneles); el badge se entera por su propio polling."""
    async def run():
        st = AppState()
        await st.update([])            # revision → 1, ok
        await st.record_error("boom: data912 timeout")
        assert st.revision == 1, "un fallo de ciclo no debe despertar a los paneles SSE"
        s = st.status(stale_after_s=30)
        assert s["last_error"].startswith("boom")
        assert s["last_error_at"] is not None
        assert s["ok"] is False, "con error pendiente, ok=False aunque la data sea reciente"
    asyncio.run(run())


def test_error_pair_is_atomic():
    """F7 (review): el par (mensaje, timestamp) debe publicarse como UNA asignación
    (tupla) — un lector concurrente nunca ve mensaje seteado con timestamp None."""
    async def run():
        st = AppState()
        await st.record_error("x")
        s = st.status(stale_after_s=30)
        # par consistente: o ambos seteados o ambos None
        assert (s["last_error"] is None) == (s["last_error_at"] is None)
        # la implementación expone el par como un solo atributo (asignación atómica)
        assert not hasattr(st, "_last_error"), \
            "el par debe vivir en UN atributo (p.ej. _error) para lectura atómica"
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


def test_status_default_threshold_comes_from_settings():
    """F9 (review): el umbral de staleness vive en UN lugar (default de status()
    desde settings), no copiado en cada endpoint (app.py health + header badge)."""
    from config.settings import settings

    async def run():
        st = AppState()
        await st.update([])
        s = st.status()                      # sin arg → default centralizado
        assert s["is_stale"] is False
        # umbral efectivo = refresh_sec * 6 (la convención existente)
        future = datetime.now() + timedelta(seconds=settings.refresh_sec * 6 + 1)
        assert st.status(now=future)["is_stale"] is True
        just_under = datetime.now() + timedelta(seconds=settings.refresh_sec * 6 - 1)
        assert st.status(now=just_under)["is_stale"] is False
    asyncio.run(run())
