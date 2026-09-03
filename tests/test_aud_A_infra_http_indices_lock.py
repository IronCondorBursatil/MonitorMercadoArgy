"""Hallazgo A-5: BCRAIndicesProvider retenía un `threading.Lock` de CLASE a
través de 4 HTTP sincrónicos, y `prefetch()` (async) lo tomaba de forma
BLOQUEANTE desde el hilo del event loop.

Si un handler sync del threadpool (típicamente `/header/cards`, que base.html
dispara cada 60s en todas las páginas) gana la carrera del gate diario y entra a
`_fetch_all()`, el próximo `await indices.prefetch(...)` del `_refresh_loop`
congela el event loop ENTERO hasta que BCRA responda (techo: 4 × timeout 10s).

`agents.md:411` ya documenta la regla opuesta para el caso análogo del BEI:
"I/O y parsing **fuera** del lock". `prefetch()` ya la cumplía; `_fetch_all()` no.
"""

import asyncio
import threading
import time

import pytest

from core.infrastructure import indices_provider as ip
from core.infrastructure.indices_provider import BCRAIndicesProvider as P


@pytest.fixture(autouse=True)
def _isolate_class_state(monkeypatch):
    prev = (P._last_attempt, P._disk_loaded, P._cache_cer, P._cache_tamar,
            P._cache_a3500, P._cache_reservas)
    P._last_attempt = None
    P._disk_loaded = True          # no tocar el disco real
    P._cache_cer = {}
    P._cache_tamar = {}
    P._cache_a3500 = {}
    P._cache_reservas = {}
    monkeypatch.setattr(ip, "_save_csv", lambda *a, **kw: None)
    yield
    (P._last_attempt, P._disk_loaded, P._cache_cer, P._cache_tamar,
     P._cache_a3500, P._cache_reservas) = prev


def test_fetch_all_no_retiene_el_lock_durante_la_red(monkeypatch):
    """Invariante: I/O de red FUERA del lock de clase."""
    lock_tomado = []

    def _fake_fetch_series(variable_id, days):
        # Lock no reentrante: si lo tiene este mismo hilo, acquire() falla.
        libre = P._lock.acquire(blocking=False)
        lock_tomado.append(not libre)
        if libre:
            P._lock.release()
        return {}

    monkeypatch.setattr(ip, "_fetch_series", _fake_fetch_series)
    P()._fetch_all()
    assert lock_tomado == [False, False, False, False], (
        "el lock de clase sigue tomado mientras corren los httpx.get de BCRA")


def test_prefetch_no_congela_el_event_loop_mientras_fetch_all_esta_en_la_red(monkeypatch):
    """El `await prefetch(...)` del refresh loop no puede quedar bloqueado detrás
    de los HTTP sincrónicos que corre un handler del threadpool."""
    en_la_red = threading.Event()
    seguir = threading.Event()

    def _fake_fetch_series(variable_id, days):
        en_la_red.set()
        seguir.wait(5.0)          # simula BCRA lento (techo real: 10s por serie)
        return {}

    monkeypatch.setattr(ip, "_fetch_series", _fake_fetch_series)

    t = threading.Thread(target=lambda: P()._fetch_all(), daemon=True)
    t.start()
    assert en_la_red.wait(5.0)

    async def _run():
        t0 = time.monotonic()
        await P().prefetch(None)   # el día ya está reclamado → debe salir ya
        return time.monotonic() - t0

    try:
        elapsed = asyncio.run(_run())
    finally:
        seguir.set()
        t.join(10.0)

    assert elapsed < 0.5, f"prefetch() bloqueó el event loop {elapsed:.2f}s"
