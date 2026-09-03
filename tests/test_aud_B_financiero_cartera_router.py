"""Auditoría lote B — router de Cartera.

  #8 las posiciones USD se valuaban con el dólar MAYORISTA (A3500) en vez del MEP
     (y la pata CABLE con CCL). Todo el panel se pondera por `market_value_ars`,
     así que el sesgo se propagaba a pesos, TIR/MD ponderadas y P&L.
  #7 `POST /cartera/holding` se tragaba los `ValueError` de `upsert_holding` con
     `except ValueError: pass` y devolvía 200: el alta fallaba en silencio.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from apps.web import cartera_store
from apps.web.app import app
from apps.web.deps import get_fx, get_repo, get_state


class _Fx:
    """Provider de FX con brecha: mayorista 1.000 · MEP 1.500 · CCL 1.600."""

    def __init__(self):
        self.calls = []

    def get_mayorista_venta(self):
        self.calls.append("mayorista")
        return 1000.0

    def get_mep_venta(self):
        self.calls.append("mep")
        return 1500.0

    def get_ccl_venta(self):
        self.calls.append("ccl")
        return 1600.0


def _state(*tickers):
    ms = []
    for tk in tickers:
        inst = NS(ticker=tk, instrument_type="HARD DOLLAR", short_name=f"ON {tk}")
        ms.append(NS(snapshot=NS(price=95.0, instrument=inst), tir=0.09, duration=2.0))
    return NS(metrics=lambda: ms)


@pytest.fixture
def client(monkeypatch):
    fx = _Fx()
    monkeypatch.setattr(cartera_store, "list_holdings",
                        lambda: [{"ticker": "YMCXD", "nominal": 10_000},
                                 {"ticker": "YMCXC", "nominal": 10_000}])
    app.dependency_overrides[get_fx] = lambda: fx
    app.dependency_overrides[get_state] = lambda: _state("YMCXD", "YMCXC")
    app.dependency_overrides[get_repo] = lambda: NS(get_all_instruments=lambda: [])
    try:
        with TestClient(app) as c:
            yield c, fx
    finally:
        for dep in (get_fx, get_state, get_repo):
            app.dependency_overrides.pop(dep, None)


def test_pata_mep_se_valua_al_mep_y_la_cable_al_ccl(client):
    c, fx = client
    r = c.get("/cartera")
    assert r.status_code == 200
    # 10.000 VN = 100 unidades × 95 USD = 9.500 USD
    assert "14,250,000" in r.text, "la pata …D debe valuarse al MEP (9.500 × 1.500)"
    assert "15,200,000" in r.text, "la pata …C debe valuarse al CCL (9.500 × 1.600)"
    assert "9,500,000" not in r.text, "no debe usar el mayorista (9.500 × 1.000)"
    assert "mayorista" not in fx.calls, f"llamó al FX oficial: {fx.calls}"


def test_alta_invalida_avisa_al_usuario_en_vez_de_fallar_en_silencio(client):
    c, _fx = client
    r = c.post("/cartera/holding",
               data={"ticker": "ZZAUD", "nominal": "0", "cost_price": "", "note": ""})
    assert r.status_code == 200
    assert "No se guard" in r.text, "el alta rechazada tiene que avisar"
    assert "nominal" in r.text


def test_alta_invalida_escapa_el_mensaje(client):
    """El ticker viene del usuario: nada de HTML crudo en el banner."""
    c, _fx = client
    r = c.post("/cartera/holding",
               data={"ticker": "   ", "nominal": "10", "cost_price": "", "note": ""})
    assert r.status_code == 200
    assert "No se guard" in r.text
    assert "<script>" not in r.text
