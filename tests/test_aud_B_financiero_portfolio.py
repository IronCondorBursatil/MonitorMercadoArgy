"""Auditoría lote B — cartera: moneda de la posición, base del P&L y FX de conversión.

Hallazgos cubiertos:
  #2 `position_currency` marcaba ARS las patas USD de ONs / provinciales / CABLE.
  #3 el P&L del header mezclaba dos bases (market value de TODAS las posiciones
     contra el costo de SOLO las que tienen precio de costo).
  #8 la cartera valuaba las posiciones USD con el dólar MAYORISTA (A3500) en vez
     del MEP (y la pata CABLE con CCL).
"""
from __future__ import annotations

import pytest

from core.domain import portfolio as pf


# ── #2 · position_currency ────────────────────────────────────────────────────

@pytest.mark.parametrize("tipo,ticker", [
    ("HARD DOLLAR", "YMCXD"),            # ON hard-dollar, pata MEP
    ("HARD DOLLAR", "YMCXC"),            # ON hard-dollar, pata CABLE
    ("PROVINCIAL HARD DOLLAR", "BA37D"),
    ("PROVINCIAL HARD DOLLAR", "CO27C"),
    ("DOLLAR LINKED", "MGCED"),          # ON dollar-linked, pata MEP
    ("BONAR", "AL30D"),
    ("BONAR", "AL30C"),                  # pata CABLE de un soberano
    ("GLOBAL", "GD30C"),
    ("BOPREAL", "BPY6C"),
])
def test_patas_mep_y_cable_de_tipos_usd_son_usd(tipo, ticker):
    assert pf.position_currency(tipo, ticker) == "USD"


@pytest.mark.parametrize("tipo,ticker", [
    ("HARD DOLLAR", "YMCXO"),   # pata pesos de una ON
    ("BONAR", "AL30"),          # pata pesos de un soberano
    ("CER", "TX26"),
    ("LECAP", "S29Y6"),
    ("ACCION", "AGROD"),        # acciones: fuera del alcance de la regla
    (None, "AL30D"),
])
def test_lo_que_no_es_pata_usd_de_un_tipo_usd_queda_en_ars(tipo, ticker):
    assert pf.position_currency(tipo, ticker) == "ARS"


def test_position_fx_leg_distingue_mep_de_cable():
    assert pf.position_fx_leg("HARD DOLLAR", "YMCXD") == "MEP"
    assert pf.position_fx_leg("HARD DOLLAR", "YMCXC") == "CABLE"
    assert pf.position_fx_leg("CER", "TX26") is None


def test_on_hard_dollar_mep_se_convierte_a_ars():
    """10.000 VN de YMCXD a USD 95 con FX 1.400 = 13.300.000 ARS, no 9.500."""
    metrics = {"YMCXD": {"price": 95.0, "currency": pf.position_currency("HARD DOLLAR", "YMCXD"),
                         "grupo": "ON", "tir": 9.0, "md": 2.0}}
    out = pf.build_portfolio([{"ticker": "YMCXD", "nominal": 10_000}], metrics,
                             fx_usd_ars=1400.0)
    p = out["positions"][0]
    assert p["currency"] == "USD"
    assert p["market_value"] == 9_500.0
    assert p["market_value_ars"] == pytest.approx(13_300_000.0)


# ── #8 · FX por pata (MEP vs CABLE) ───────────────────────────────────────────

def test_pata_cable_se_convierte_con_ccl_no_con_mep():
    metrics = {
        "AL30D": {"price": 70.0, "currency": "USD", "grupo": "Soberano"},
        "AL30C": {"price": 70.0, "currency": "USD", "grupo": "Soberano"},
    }
    out = pf.build_portfolio(
        [{"ticker": "AL30D", "nominal": 10_000}, {"ticker": "AL30C", "nominal": 10_000}],
        metrics, fx_usd_ars=1500.0, fx_cable_ars=1600.0,
    )
    pos = {p["ticker"]: p for p in out["positions"]}
    assert pos["AL30D"]["market_value_ars"] == pytest.approx(7_000 * 1500.0)
    assert pos["AL30C"]["market_value_ars"] == pytest.approx(7_000 * 1600.0)
    assert out["summary"]["fx_cable_ars"] == 1600.0


def test_sin_ccl_la_pata_cable_cae_al_mep():
    metrics = {"AL30C": {"price": 70.0, "currency": "USD", "grupo": "Soberano"}}
    out = pf.build_portfolio([{"ticker": "AL30C", "nominal": 10_000}], metrics,
                             fx_usd_ars=1500.0)
    assert out["positions"][0]["market_value_ars"] == pytest.approx(7_000 * 1500.0)


# ── #3 · base del P&L ─────────────────────────────────────────────────────────

_M = {
    "AL30D": {"price": 70.0, "currency": "USD", "grupo": "Soberano"},
    "TX26":  {"price": 1500.0, "currency": "ARS", "grupo": "CER"},
}


def test_pnl_ignora_las_posiciones_sin_precio_de_costo():
    """AL30D 10.000 VN costo 60 (precio 70, FX 1.400) + TX26 1.000.000 VN SIN costo.
    El P&L medible es (70-60)×100×1.400 = 1.400.000 (+16,67%), no 16,4 M / +195%."""
    holdings = [
        {"ticker": "AL30D", "nominal": 10_000, "cost_price": 60.0},
        {"ticker": "TX26", "nominal": 1_000_000},        # sin cost_price
    ]
    s = pf.build_portfolio(holdings, _M, fx_usd_ars=1400.0)["summary"]
    assert s["total_market_value_ars"] == pytest.approx(9_800_000.0 + 15_000_000.0)
    assert s["pnl_ars"] == pytest.approx(1_400_000.0)
    assert s["pnl_pct"] == pytest.approx(70.0 / 60.0 - 1.0)
    assert s["cost_coverage"] == pytest.approx(9_800_000.0 / 24_800_000.0)


def test_pnl_ignora_las_posiciones_con_costo_pero_sin_precio_vivo():
    """Simetría inversa: una tenencia con costo y sin precio en el snapshot
    sumaba al costo y no al market value → SUBESTIMABA el P&L."""
    holdings = [
        {"ticker": "AL30D", "nominal": 10_000, "cost_price": 60.0},
        {"ticker": "ZZZ", "nominal": 1_000_000, "cost_price": 100.0},   # sin precio
    ]
    s = pf.build_portfolio(holdings, _M, fx_usd_ars=1400.0)["summary"]
    assert s["pnl_ars"] == pytest.approx(1_400_000.0)
    assert s["pnl_pct"] == pytest.approx(70.0 / 60.0 - 1.0)


def test_sin_ninguna_posicion_con_costo_el_pnl_es_none():
    s = pf.build_portfolio([{"ticker": "TX26", "nominal": 1_000}], _M,
                           fx_usd_ars=1400.0)["summary"]
    assert s["pnl_ars"] is None and s["pnl_pct"] is None
    assert s["cost_coverage"] is None
