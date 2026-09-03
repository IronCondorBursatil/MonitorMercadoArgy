"""Auditoría D2 — `supervise()`: el presupuesto anti-livelock debe contar
cancelaciones **seguidas**, no acumularlas de por vida.

`consecutive_cancels` sólo volvía a 0 cuando la corrida terminaba por excepción o
por retorno — algo que los 5 loops reales (todos `while True` con `except
CancelledError: raise` + `except Exception` catch-all) NUNCA hacen. O sea: para
producción el contador era acumulado de por vida del proceso, y cinco
cancelaciones espurias espaciadas por DÍAS de operación sana mataban el loop igual
que cinco seguidas en un milisegundo — reviviendo el incidente del 2026-09-01
(app sirviendo el mismo snapshot ~22hs) con el supervisor instalado.
"""

from __future__ import annotations

import asyncio

import pytest

from apps.web.supervisor import supervise


def _run(coro):
    return asyncio.run(coro)


def test_cancelaciones_espurias_espaciadas_no_agotan_el_presupuesto():
    """8 horas sanas entre cancelación y cancelación ⇒ no son una ráfaga: el
    contador tiene que arrancar de cero en cada una y el loop seguir vivo."""
    stopping = asyncio.Event()
    clock = {"t": 0.0}
    calls = 0

    async def child():
        nonlocal calls
        calls += 1
        if calls > 6:                    # 6 cancelaciones espaciadas ya alcanzan
            stopping.set()
            return
        clock["t"] += 8 * 3600.0         # corrió 8hs sanas antes de la cancelación
        raise asyncio.CancelledError()

    async def fake_sleep(_d):
        return None

    async def main():
        await supervise("refresh", child, stopping=stopping, base_delay=0,
                        max_consecutive_cancels=3, healthy_after=60.0,
                        _sleep=fake_sleep, _monotonic=lambda: clock["t"])

    _run(main())
    assert calls == 7, "el supervisor se rindió pese a que cada corrida fue sana"


def test_rafaga_instantanea_de_cancelaciones_sigue_cortando():
    """La salvaguarda anti-livelock NO se debilita: sin corridas sanas de por medio
    (el event loop cerrándose por fuera del lifespan), sigue rindiéndose."""
    stopping = asyncio.Event()
    clock = {"t": 0.0}
    calls = 0

    async def child():
        nonlocal calls
        calls += 1
        clock["t"] += 0.5                # muy por debajo de healthy_after
        raise asyncio.CancelledError()

    async def fake_sleep(_d):
        return None

    async def main():
        with pytest.raises(asyncio.CancelledError):
            await supervise("refresh", child, stopping=stopping, base_delay=0,
                            max_consecutive_cancels=3, healthy_after=60.0,
                            _sleep=fake_sleep, _monotonic=lambda: clock["t"])

    _run(main())
    assert calls == 3


def test_la_rendicion_reporta_on_crash():
    """La muerte DEFINITIVA es la que más importa reportar: el `raise` estaba antes
    del bloque de `on_crash`, así que la única caída irreversible no llegaba nunca
    a `AppState.record_error` (ni al badge, ni a /api/health)."""
    stopping = asyncio.Event()
    seen: list[tuple[str, str]] = []
    calls = 0

    async def child():
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    async def on_crash(name, reason):
        seen.append((name, reason))

    async def main():
        with pytest.raises(asyncio.CancelledError):
            await supervise("refresh", child, stopping=stopping, base_delay=0,
                            on_crash=on_crash, max_consecutive_cancels=2)

    _run(main())
    assert calls == 2
    assert seen, "la rendición definitiva no reportó nada por on_crash"
    assert seen[-1][0] == "refresh"
    assert "rind" in seen[-1][1].lower(), seen[-1][1]
