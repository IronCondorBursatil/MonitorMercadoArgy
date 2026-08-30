"""Store de histórico FCI (fci_history.py): acumula vcp/ccp/patrimonio por fondo
desde ArgentinaDatos y deriva el flujo neto orgánico (Δcuotapartes × VCP).

El flujo real de un FCI no se publica; se infiere de la variación de cuotapartes en
circulación (ccp), que cambia SOLO por suscripciones/rescates (no por precio).
"""

import os
from datetime import date

import pytest

class MockResponse:
    def __init__(self, json_data):
        self._json = json_data
    def raise_for_status(self):
        pass
    def json(self):
        return self._json


from core.infrastructure.fci_history import (
    FCIHistoryStore, net_flow_series, record_from_ard,
)


@pytest.fixture
def store(tmp_path):
    return FCIHistoryStore(os.path.join(str(tmp_path), "fh.db"))


def _row(fondo, vcp, ccp, patrimonio):
    return {"fondo": fondo, "vcp": vcp, "ccp": ccp, "patrimonio": patrimonio}


def test_record_and_get_series(store):
    n = store.record_snapshot(
        [_row("Alpha Pesos - Clase A", 100.0, 1_000.0, 100_000.0),
         _row("FBA Renta Pesos", 50.0, 2_000.0, 100_000.0)],
        date(2026, 6, 2))
    assert n == 2
    s = store.get_series("alpha pesos - clase a")        # case/accent-insensitive
    assert s == {date(2026, 6, 2): {"vcp": 100.0, "ccp": 1000.0, "patrimonio": 100000.0}}
    assert store.get_series("nope") == {}


def test_record_idempotent_per_day(store):
    store.record_snapshot([_row("X", 10.0, 100.0, 1000.0)], date(2026, 6, 2))
    store.record_snapshot([_row("X", 11.0, 110.0, 1210.0)], date(2026, 6, 2))   # mismo día
    assert store.get_series("X") == {date(2026, 6, 2): {"vcp": 11.0, "ccp": 110.0, "patrimonio": 1210.0}}


def test_skips_rows_without_ccp_or_name(store):
    n = store.record_snapshot(
        [_row("", 10.0, 100.0, 1000.0),          # sin nombre
         _row("Y", 10.0, None, 1000.0),          # sin ccp → no sirve para flujos
         _row("Z", 10.0, 500.0, 5000.0)],
        date(2026, 6, 2))
    assert n == 1
    assert store.get_series("Z")[date(2026, 6, 2)]["ccp"] == 500.0


def test_persists_across_instances(store):
    store.record_snapshot([_row("AE", 80.0, 10.0, 800.0)], date(2026, 6, 2))
    reopened = FCIHistoryStore(store._db_path)
    assert reopened.get_series("AE")[date(2026, 6, 2)]["vcp"] == 80.0


def test_net_flow_series_uses_ccp_delta_times_vcp():
    """Flujo entre días consecutivos = Δccp × vcp del día. Capta suscripción/rescate
    SIN contaminarse por la apreciación del VCP."""
    series = {
        date(2026, 6, 2): {"vcp": 100.0, "ccp": 1000.0, "patrimonio": 100000.0},
        date(2026, 6, 3): {"vcp": 110.0, "ccp": 1000.0, "patrimonio": 110000.0},  # subió precio, sin flujo
        date(2026, 6, 4): {"vcp": 110.0, "ccp": 1200.0, "patrimonio": 132000.0},  # +200 cuotapartes
        date(2026, 6, 5): {"vcp": 120.0, "ccp": 1100.0, "patrimonio": 132000.0},  # -100 cuotapartes
    }
    flows = net_flow_series(series)
    assert flows[date(2026, 6, 3)] == pytest.approx(0.0)            # solo precio → sin flujo
    assert flows[date(2026, 6, 4)] == pytest.approx(200 * 110.0)    # +22.000
    assert flows[date(2026, 6, 5)] == pytest.approx(-100 * 120.0)   # -12.000
    assert date(2026, 6, 2) not in flows                            # 1er punto no tiene previo


def test_net_flow_series_empty_or_single_point():
    assert net_flow_series({}) == {}
    assert net_flow_series({date(2026, 6, 2): {"vcp": 1.0, "ccp": 1.0, "patrimonio": 1.0}}) == {}


def test_net_flow_series_discards_implausible_ccp_jump():
    """A10: un renombre/colisión de fondo en ArgentinaDatos parte la serie de ccp y
    produce un Δccp gigante. El guard de continuidad descarta ese punto (no se lee como
    un flujo espurio de millones), pero NO borra el flujo orgánico siguiente."""
    from core.infrastructure.fci_history import _NET_FLOW_MAX_JUMP
    big = 1000.0 * (_NET_FLOW_MAX_JUMP + 2.0)        # ratio = umbral+1 > umbral → implausible
    series = {
        date(2026, 6, 2): {"vcp": 100.0, "ccp": 1000.0, "patrimonio": 100_000.0},
        date(2026, 6, 3): {"vcp": 100.0, "ccp": big, "patrimonio": big * 100},          # discontinuidad
        date(2026, 6, 4): {"vcp": 100.0, "ccp": big + 200.0, "patrimonio": (big + 200) * 100},  # +200 orgánico
    }
    flows = net_flow_series(series)
    assert date(2026, 6, 3) not in flows                            # salto implausible → descartado
    assert flows[date(2026, 6, 4)] == pytest.approx(200 * 100.0)    # el flujo siguiente SÍ se conserva


def test_net_flow_series_keeps_large_but_plausible_jump():
    """Un salto grande pero por debajo del umbral (un fondo chico que recibe una
    suscripción fuerte) NO se borra silenciosamente."""
    from core.infrastructure.fci_history import _NET_FLOW_MAX_JUMP
    ratio = _NET_FLOW_MAX_JUMP - 0.5                  # plausible: < umbral
    bumped = 1000.0 * (1.0 + ratio)
    series = {
        date(2026, 6, 2): {"vcp": 50.0, "ccp": 1000.0, "patrimonio": 50_000.0},
        date(2026, 6, 3): {"vcp": 50.0, "ccp": bumped, "patrimonio": bumped * 50},
            return MockResponse([{"fondo": "RF Dos", "vcp": 50.0, "ccp": 2000.0,
                     "patrimonio": 100000.0, "fecha": "2026-06-02"}])
        return MockResponse([])
    monkeypatch.setattr("httpx.get", fake_get)

    n = record_from_ard(store, date(2026, 6, 2))
    assert n == 2
    assert store.get_series("MM Uno")[date(2026, 6, 2)]["ccp"] == 1000.0
    assert store.get_series("RF Dos")[date(2026, 6, 2)]["vcp"] == 50.0
