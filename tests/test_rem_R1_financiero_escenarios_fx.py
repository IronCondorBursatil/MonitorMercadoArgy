"""Auditoría R1 — /cartera y /escenarios valúan el MISMO libro con el MISMO FX.

El hallazgo #8 ("la cartera valuaba las posiciones en dólares al mayorista/A3500,
que es el oficial y tiene brecha contra MEP/CCL") se arregló sólo en
`routers/cartera.py`. `routers/escenarios.py::_cartera_pnl` quedó llamando a
`fx.get_mayorista_venta()` y a `build_portfolio` SIN `fx_cable_ars`, así que los
dos paneles pasaron a contradecirse: con una brecha del 5% el P&L de escenarios
quedaba ~5% corrido respecto del de la cartera, y las patas CABLE valuadas al
oficial en vez del CCL.

Acá se fija que ambos routers usan `cartera._fx_rates` (MEP para las …D, CCL para
las …C) — es el mismo helper, no una copia.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from apps.web.routers import cartera as cartera_router
from apps.web.routers import escenarios as escenarios_router
from core.domain.models import Instrument, MarketSnapshot

_MEP, _CCL, _MAYORISTA = 1400.0, 1500.0, 1000.0


class _Fx:
    """Brecha real: MEP 1400 / CCL 1500 / mayorista 1000."""

    def get_mep_venta(self):
        return _MEP

    def get_ccl_venta(self):
        return _CCL

    def get_mayorista_venta(self):
        return _MAYORISTA


def _metric(ticker: str, itype: str, price: float, md: float):
    inst = Instrument(ticker=ticker, short_name=ticker, instrument_type=itype,
                      maturity_date=date(2030, 1, 1), cashflows=[])
    return SimpleNamespace(snapshot=MarketSnapshot(instrument=inst, price=price),
                           duration=md, tir=0.10)


class _State:
    def metrics(self):
        return [
            _metric("AL30D", "BONAR", 70.0, 3.0),    # pata MEP
            _metric("AL30C", "BONAR", 69.0, 3.0),    # pata CABLE
            _metric("S30S6", "LECAP", 130.0, 0.4),   # pesos
        ]


_HOLDINGS = [
    {"ticker": "AL30D", "nominal": 100_000, "cost_price": 60.0},
    {"ticker": "AL30C", "nominal": 100_000, "cost_price": 60.0},
    {"ticker": "S30S6", "nominal": 100_000, "cost_price": 120.0},
]


@pytest.fixture(autouse=True)
def _holdings(monkeypatch):
    monkeypatch.setattr("apps.web.cartera_store.list_holdings", lambda: list(_HOLDINGS))


def _pnl_escenarios():
    return escenarios_router._cartera_pnl(_State(), _Fx(), 0.0, 0.0)


def _valor_cartera():
    """El valor del libro tal como lo publica /cartera (misma valuación)."""
    from core.domain import portfolio
    mep, ccl = cartera_router._fx_rates(_Fx())
    pf = portfolio.build_portfolio(_HOLDINGS, cartera_router._metrics_by_ticker(_State()),
                                   fx_usd_ars=mep, fx_cable_ars=ccl)
    return pf["summary"]["total_market_value_ars"]


def test_escenarios_valua_el_libro_igual_que_cartera():
    """Escenario nulo (0 bps, 0% FX): la base del P&L es el valor del libro."""
    assert _pnl_escenarios()["base_value_ars"] == pytest.approx(_valor_cartera())


def test_escenarios_no_valua_al_mayorista():
    """Guarda directa contra la regresión: con el oficial (1000) la base sería
    ~30% más chica que con MEP/CCL."""
    base = _pnl_escenarios()["base_value_ars"]
    al_oficial = (100_000 / 100 * 70.0 + 100_000 / 100 * 69.0) * _MAYORISTA + 100_000 / 100 * 130.0
    assert base > al_oficial * 1.2
    assert base == pytest.approx(
        100_000 / 100 * 70.0 * _MEP + 100_000 / 100 * 69.0 * _CCL + 100_000 / 100 * 130.0)


def test_la_pata_cable_de_escenarios_va_al_ccl():
    """Sin `fx_cable_ars` la …C caía al MEP: 1500 vs 1400 por dólar."""
    pos = {p["ticker"]: p for p in _pnl_escenarios()["positions"]}
    assert pos["AL30C"]["market_value_ars"] == pytest.approx(100_000 / 100 * 69.0 * _CCL)
    assert pos["AL30D"]["market_value_ars"] == pytest.approx(100_000 / 100 * 70.0 * _MEP)


def test_escenarios_reusa_el_helper_de_cartera_no_una_copia():
    """Guarda estructural: una sola definición de las puntas de FX."""
    import inspect
    src = inspect.getsource(escenarios_router._cartera_pnl)
    assert "_fx_rates" in src
    assert "get_mayorista_venta" not in src
