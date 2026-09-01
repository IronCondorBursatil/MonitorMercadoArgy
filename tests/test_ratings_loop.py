"""Loop diario de calificaciones FIX SCR en el lifespan (`apps/web/app.py`).

Cero red y cero SQLite: el scraper (`fix_ratings.fetch_listado`) y el store
(`get_ratings_history_store`) se reemplazan por dobles. Lo que se ejercita es el
CABLEADO — cuándo el loop decide scrapear, qué le pasa al store, qué hace cuando el
sitio falla y que el corte nuevo invalide el cache del panel.

Los tests async corren con `asyncio.run` (no hay pytest-asyncio en el gate), mismo
patrón que `tests/test_async_http.py`. El loop es infinito: cada test lo arranca como
task, espera con `_esperar` a que el trabajo observable ocurra y lo cancela en el
`finally` — nunca se lo deja vivo entre tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from datetime import date
from functools import lru_cache

import pytest
from fastapi.testclient import TestClient

from apps.web import app as app_mod
from core.infrastructure import fix_ratings
from core.infrastructure.fix_ratings import FixRow


# --------------------------------------------------------------------------- #
# Dobles
# --------------------------------------------------------------------------- #
class _FakeStore:
    """Store de calificaciones en memoria: cuenta las llamadas para poder afirmar
    que el loop NO scrapea cuando el corte del día ya está."""

    def __init__(self, fecha_actual=None, resultado=None):
        self.fecha_actual = fecha_actual
        self.resultado = resultado or {"status": "ok", "fecha": "2026-08-31", "rows": 2,
                                       "changes": 1, "up": 1, "down": 0, "watch": 0,
                                       "reason": None}
        self.latest_calls = 0
        self.record_calls: list[tuple] = []

    def latest_fecha(self):
        self.latest_calls += 1
        return self.fecha_actual

    def record_corte(self, rows, hoy):
        self.record_calls.append((rows, hoy))
        return self.resultado


def _fila(entidad: str, rating: str, *, sector: str = "Energía",
          area: str = "Finanzas Corporativas", persp: str = "Estable") -> FixRow:
    return FixRow(entidad=entidad, fecha=date(2026, 8, 6), pais="Argentina", area=area,
                  sector=sector, tipo="Emisor", rating_cp="A1(arg)", rating_lp=rating,
                  perspectiva=persp, estado="Confirma")


def _cablear(monkeypatch, store, filas=None, error=None):
    """Reemplaza el store singleton y el scraper. `error` hace fallar a `fetch_listado`."""
    llamadas = {"fetch": 0}
    monkeypatch.setattr("core.infrastructure.ratings_history.get_ratings_history_store",
                        lambda: store)

    def _fake_fetch(*a, **kw):
        llamadas["fetch"] += 1
        if error is not None:
            raise error
        return list(filas or [])

    monkeypatch.setattr(fix_ratings, "fetch_listado", _fake_fetch)
    return llamadas


async def _esperar(pred, timeout: float = 3.0) -> bool:
    """True cuando `pred()` se cumple dentro del timeout (polling cortito: el trabajo
    del loop pasa por `to_thread`, así que no alcanza con un solo `sleep(0)`)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


async def _cancelar(task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------- #
# Wiring en el lifespan
# --------------------------------------------------------------------------- #
def _stub_loops(monkeypatch, evento: threading.Event):
    """Todos los loops del lifespan por no-ops; el de ratings avisa por `evento`.

    Sin esto, sacar MONITOR_DISABLE_LOOPS para probar el wiring arrancaría refresh/BEI
    /price-history contra la red REAL."""
    async def _noop(app):
        return None

    async def _spy(app):
        evento.set()
        await asyncio.sleep(3600)   # queda viva hasta el cancel del lifespan

    for nombre in ("_startup_reconcile", "_refresh_loop", "_options_loop", "_bei_loop",
                   "_price_history_loop"):
        monkeypatch.setattr(app_mod, nombre, _noop)
    monkeypatch.setattr(app_mod, "_ratings_loop", _spy)


def test_lifespan_registra_el_ratings_loop(monkeypatch):
    evento = threading.Event()
    _stub_loops(monkeypatch, evento)
    monkeypatch.delenv("MONITOR_DISABLE_LOOPS", raising=False)
    with TestClient(app_mod.app):
        assert evento.wait(5.0), "el lifespan no arrancó _ratings_loop"


def test_disable_loops_no_arranca_el_ratings_loop(monkeypatch):
    """En tests (conftest setea MONITOR_DISABLE_LOOPS=1) el loop NO puede arrancar:
    scrapearía fixscr.com desde la suite."""
    evento = threading.Event()
    _stub_loops(monkeypatch, evento)
    monkeypatch.setenv("MONITOR_DISABLE_LOOPS", "1")
    with TestClient(app_mod.app):
        assert not evento.wait(0.3)


# --------------------------------------------------------------------------- #
# Comportamiento del tick
# --------------------------------------------------------------------------- #
def test_no_scrapea_si_ya_hay_corte_de_hoy(monkeypatch):
    """Restart-safe: reiniciar el server 5 veces en el día no dispara 5 scrapes."""
    store = _FakeStore(fecha_actual=date.today().isoformat())
    llamadas = _cablear(monkeypatch, store, filas=[_fila("Pampa", "AA(arg)")])

    async def run():
        task = asyncio.create_task(app_mod._ratings_loop(None))
        try:
            assert await _esperar(lambda: store.latest_calls > 0), "el tick no corrió"
            await asyncio.sleep(0.05)      # margen para que un scrape indebido aparezca
            assert llamadas["fetch"] == 0
            assert store.record_calls == []
        finally:
            await _cancelar(task)

    asyncio.run(run())


def test_scrapea_y_graba_si_falta_el_corte_de_hoy(monkeypatch):
    store = _FakeStore(fecha_actual="2026-08-20")
    filas = [_fila("Pampa Energía S.A.", "AAA(arg)"),
             _fila("Banco Galicia", "AA+(arg)", area="Entidades Financieras",
                   sector="Bancos", persp="Positiva"),
             # Emisión estructurada: `mejor_fila_por_entidad` la deja afuera → el
             # payload del store no puede traerla.
             _fila("Fideicomiso XX", "AAAsf(arg)")]
    llamadas = _cablear(monkeypatch, store, filas=filas)

    async def run():
        task = asyncio.create_task(app_mod._ratings_loop(None))
        try:
            assert await _esperar(lambda: store.record_calls), "no grabó el corte"
            assert llamadas["fetch"] == 1
            rows, hoy = store.record_calls[0]
            assert hoy == date.today()
            assert set(rows) == {"Pampa Energía S.A.", "Banco Galicia"}
            assert rows["Banco Galicia"] == {"rating": "AA+(arg)", "perspectiva": "Positiva",
                                             "area": "Entidades Financieras", "sector": "Bancos"}
        finally:
            await _cancelar(task)

    asyncio.run(run())


def test_error_del_scraper_no_tumba_el_loop(monkeypatch):
    """El sitio caído es un día sin corte, no un lifespan roto: el panel tiene que
    seguir sirviendo el último corte bueno y el loop reintentar al tick siguiente."""
    store = _FakeStore(fecha_actual=None)
    llamadas = _cablear(monkeypatch, store, error=RuntimeError("fixscr 503"))

    async def run():
        task = asyncio.create_task(app_mod._ratings_loop(None))
        try:
            assert await _esperar(lambda: llamadas["fetch"] > 0)
            await asyncio.sleep(0.05)
            assert not task.done(), "la excepción del scraper tumbó el loop"
            assert store.record_calls == []
        finally:
            await _cancelar(task)

    asyncio.run(run())


def test_corte_nuevo_invalida_el_cache_de_ratings(monkeypatch):
    """Sin invalidar, el panel serviría el corte viejo hasta el próximo reinicio."""
    store = _FakeStore(fecha_actual=None)
    _cablear(monkeypatch, store, filas=[_fila("Pampa", "AA(arg)")])
    invalidaciones = []
    monkeypatch.setattr(app_mod, "_invalidate_ratings_cache",
                        lambda: invalidaciones.append(1))

    async def run():
        task = asyncio.create_task(app_mod._ratings_loop(None))
        try:
            assert await _esperar(lambda: invalidaciones), "no invalidó el cache"
        finally:
            await _cancelar(task)

    asyncio.run(run())


@pytest.mark.parametrize("status", ["noop", "discarded", "error"])
def test_corte_no_grabado_no_invalida_el_cache(monkeypatch, status):
    """`noop`/`discarded`/`error` = el read-path no cambió; tirar el cache sería
    releer el CSV + rearmar el matcher para nada."""
    store = _FakeStore(fecha_actual=None,
                       resultado={"status": status, "fecha": "2026-08-31", "rows": 0,
                                  "changes": 0, "reason": "x"})
    _cablear(monkeypatch, store, filas=[_fila("Pampa", "AA(arg)")])
    invalidaciones = []
    monkeypatch.setattr(app_mod, "_invalidate_ratings_cache",
                        lambda: invalidaciones.append(1))

    async def run():
        task = asyncio.create_task(app_mod._ratings_loop(None))
        try:
            assert await _esperar(lambda: store.record_calls)
            await asyncio.sleep(0.05)
            assert invalidaciones == []
        finally:
            await _cancelar(task)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Invalidación del cache del read-path
# --------------------------------------------------------------------------- #
def test_invalidate_usa_el_hook_del_modulo_si_existe(monkeypatch):
    from core.infrastructure import ratings
    llamado = []
    monkeypatch.setattr(ratings, "invalidate_cache", lambda: llamado.append(1),
                        raising=False)
    app_mod._invalidate_ratings_cache()
    assert llamado == [1]


def test_invalidate_sin_hook_limpia_los_lru_cache(monkeypatch):
    """Fallback: si `ratings` no expone hook, se barren los `cache_clear` del módulo.
    Se inyecta una función cacheada propia para no acoplar el test a QUÉ funciones
    de `ratings` están memoizadas hoy."""
    from core.infrastructure import ratings
    monkeypatch.delattr(ratings, "invalidate_cache", raising=False)

    @lru_cache(maxsize=8)
    def _cacheada(x):
        return x

    _cacheada(1)
    assert _cacheada.cache_info().currsize == 1
    monkeypatch.setattr(ratings, "_cacheada_de_test", _cacheada, raising=False)
    app_mod._invalidate_ratings_cache()
    assert _cacheada.cache_info().currsize == 0
