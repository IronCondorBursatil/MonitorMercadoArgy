"""Tests del supervisor de las tasks de fondo (`apps/web/supervisor.py`).

Regresión del incidente 2026-09-01: `_refresh_loop` murió a las 12:45 UTC por una
cancelación espuria y NADIE la reinició — la app sirvió el mismo snapshot ~22hs.
El caso que importa es `test_reinicia_tras_cancelacion_espuria`.

Sin pytest-asyncio en el repo: cada test maneja su propio loop con `asyncio.run`.
"""

from __future__ import annotations

import asyncio

import pytest

from apps.web.supervisor import supervise


def _run(coro):
    return asyncio.run(coro)


def test_reinicia_tras_excepcion():
    """Una excepción del loop no puede ser terminal: el supervisor lo vuelve a arrancar."""
    stopping = asyncio.Event()
    calls = 0

    async def child():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("boom")
        stopping.set()          # 3ra vuelta: cortamos el test

    async def main():
        await supervise("t", child, stopping=stopping, base_delay=0)

    _run(main())
    assert calls == 3


def test_reinicia_tras_cancelacion_espuria():
    """EL BUG DE PRODUCCIÓN: una CancelledError que NO viene del shutdown mataba la
    task en silencio. Con el supervisor, se absorbe y el loop se reinicia."""
    stopping = asyncio.Event()
    calls = 0

    async def child():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError()
        stopping.set()

    async def main():
        await supervise("t", child, stopping=stopping, base_delay=0)

    _run(main())
    assert calls == 2


def test_reinicia_si_el_loop_retorna():
    """Un `while True` no debería retornar nunca; si lo hace, es una caída igual."""
    stopping = asyncio.Event()
    calls = 0

    async def child():
        nonlocal calls
        calls += 1
        if calls >= 2:
            stopping.set()

    async def main():
        await supervise("t", child, stopping=stopping, base_delay=0)

    _run(main())
    assert calls == 2


def test_no_reinicia_en_shutdown():
    """Shutdown real: el lifespan setea `stopping` y DESPUÉS cancela. El supervisor
    tiene que honrar la cancelación (no reintentar) y propagar CancelledError."""
    stopping = asyncio.Event()
    calls = 0

    async def child():
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()        # cuelga para siempre

    async def main():
        task = asyncio.create_task(
            supervise("t", child, stopping=stopping, base_delay=0))
        await asyncio.sleep(0)              # dejar que arranque el child
        stopping.set()                      # orden del lifespan: flag ANTES del cancel
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(main())
    assert calls == 1                        # nunca se reinició


def test_reporta_el_motivo_de_la_caida():
    """El motivo va al callback (`AppState.record_error`) para que el header/health
    muestren la caída — el incidente fue MUDO, y eso es lo que lo hizo durar 22hs."""
    stopping = asyncio.Event()
    seen: list[tuple[str, str]] = []
    calls = 0

    async def child():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("data912 explotó")
        if calls == 2:
            raise asyncio.CancelledError()
        stopping.set()

    async def on_crash(name, reason):
        seen.append((name, reason))

    async def main():
        await supervise("refresh", child, stopping=stopping,
                        on_crash=on_crash, base_delay=0)

    _run(main())
    assert [n for n, _ in seen] == ["refresh", "refresh"]
    assert "ValueError" in seen[0][1] and "data912 explotó" in seen[0][1]
    assert "cancel" in seen[1][1].lower()


def test_backoff_crece_con_fallos_inmediatos():
    """Un loop que revienta al instante no debe convertirse en un busy-loop: el
    reintento va con backoff exponencial acotado por `max_delay`."""
    stopping = asyncio.Event()
    delays: list[float] = []
    calls = 0

    async def child():
        nonlocal calls
        calls += 1
        if calls >= 4:
            stopping.set()
            return
        raise RuntimeError("boom")

    async def fake_sleep(d):
        delays.append(d)

    async def main():
        await supervise("t", child, stopping=stopping, base_delay=1.0,
                        max_delay=4.0, _sleep=fake_sleep)

    _run(main())
    assert delays == [1.0, 2.0, 4.0]        # 8.0 quedaría capado por max_delay


def test_backoff_se_resetea_si_el_loop_corrio_sano():
    """Una caída aislada tras horas sanas debe reintentar YA (base_delay), no con el
    backoff acumulado de un arranque fallido — es el caso real del incidente."""
    stopping = asyncio.Event()
    delays: list[float] = []
    calls = 0
    clock = {"t": 0.0}

    async def child():
        nonlocal calls
        calls += 1
        if calls >= 3:
            stopping.set()
            return
        clock["t"] += 3600.0                # el loop corrió 1 hora antes de caerse
        raise RuntimeError("boom")

    async def fake_sleep(d):
        delays.append(d)

    async def main():
        await supervise("t", child, stopping=stopping, base_delay=1.0, max_delay=60.0,
                        healthy_after=60.0, _sleep=fake_sleep,
                        _monotonic=lambda: clock["t"])

    _run(main())
    assert delays == [1.0, 1.0]             # nunca escaló: cada corrida fue "sana"


def test_corta_ante_cancelaciones_repetidas():
    """Salvaguarda anti-livelock: si el event loop se está cerrando y cancela sin que
    `stopping` esté seteado, el supervisor no puede quedar absorbiendo cancelaciones
    para siempre — tras N seguidas se rinde y propaga."""
    stopping = asyncio.Event()
    calls = 0

    async def child():
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    async def main():
        with pytest.raises(asyncio.CancelledError):
            await supervise("t", child, stopping=stopping, base_delay=0,
                            max_consecutive_cancels=3)

    _run(main())
    assert calls == 3
