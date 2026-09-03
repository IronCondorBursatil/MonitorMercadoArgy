"""Hallazgo A-2: DolarAPIProvider sin negative caching.

Con dolarapi caído y el TTL vencido, CADA `get_quote()` reintentaba la red. Las
llamadas son por instrumento dentro del motor (DolarLinked/HardDollar → ~342 por
ciclo), cada una con `httpx.get(timeout=10.0)` bajo un lock de clase: el ciclo de
5s pasa a decenas de minutos y bloquea además los handlers sync (/header/cards).

El resto de los providers ya tiene el patrón (REMProvider.FAIL_COOLDOWN,
CAFCIProvider._last_fail_ts, BondTerminalProvider._FAIL_TTL_S).
"""

import time

import pytest

from core.infrastructure import fx_provider as fx_mod
from core.infrastructure.fx_provider import DolarAPIProvider


@pytest.fixture(autouse=True)
def _reset_fx_class_state():
    prev = (DolarAPIProvider._cache, DolarAPIProvider._last_fetch_ts,
            getattr(DolarAPIProvider, "_last_fail_ts", 0.0))
    DolarAPIProvider._cache = {}
    DolarAPIProvider._last_fetch_ts = 0.0
    DolarAPIProvider._last_fail_ts = 0.0
    yield
    (DolarAPIProvider._cache, DolarAPIProvider._last_fetch_ts,
     DolarAPIProvider._last_fail_ts) = prev


def _boom_counter(monkeypatch):
    calls = {"n": 0}

    def _boom(*a, **kw):
        calls["n"] += 1
        raise TimeoutError("dolarapi down")

    monkeypatch.setattr(fx_mod.httpx, "get", _boom)
    return calls


def test_fetch_frio_no_hace_un_get_por_instrumento(monkeypatch):
    """Cache fría + upstream caído: 50 lecturas → 1 sola salida a la red."""
    calls = _boom_counter(monkeypatch)
    fx = DolarAPIProvider()
    for _ in range(50):
        assert fx.get_mayorista_venta() is None
    assert calls["n"] == 1


def test_fetch_con_ttl_vencido_no_hace_un_get_por_instrumento(monkeypatch):
    """Cache tibia con TTL vencido + upstream caído: mismo cooldown."""
    calls = _boom_counter(monkeypatch)
    DolarAPIProvider._cache = {"mayorista": {"venta": 1000.0, "compra": 990.0}}
    DolarAPIProvider._last_fetch_ts = time.time() - (DolarAPIProvider.TTL_SECONDS + 10)
    fx = DolarAPIProvider()
    for _ in range(50):
        assert fx.get_mayorista_venta() == 1000.0   # sigue sirviendo stale
    assert calls["n"] == 1


def test_cooldown_vencido_vuelve_a_intentar(monkeypatch):
    """El negative caching no puede ser permanente: pasado el cooldown reintenta."""
    calls = _boom_counter(monkeypatch)
    fx = DolarAPIProvider()
    fx.get_mayorista_venta()
    assert calls["n"] == 1
    DolarAPIProvider._last_fail_ts = time.time() - (DolarAPIProvider.FAIL_COOLDOWN + 1)
    fx.get_mayorista_venta()
    assert calls["n"] == 2


def test_exito_limpia_el_sello_de_fallo(monkeypatch):
    """Tras un éxito, el cooldown de fallo no debe seguir bloqueando."""
    _boom_counter(monkeypatch)
    fx = DolarAPIProvider()
    fx.get_mayorista_venta()
    assert DolarAPIProvider._last_fail_ts > 0.0

    class _Resp:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return [{"casa": "mayorista", "nombre": "May", "compra": 1, "venta": 2}]

    monkeypatch.setattr(fx_mod.httpx, "get", lambda *a, **kw: _Resp())
    DolarAPIProvider._last_fail_ts = time.time() - (DolarAPIProvider.FAIL_COOLDOWN + 1)
    assert fx.get_mayorista_venta() == 2.0
    assert DolarAPIProvider._last_fail_ts == 0.0
