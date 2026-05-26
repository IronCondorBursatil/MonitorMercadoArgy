"""Tests del motor de cartera (core.domain.portfolio) — puro, sin red/Excel."""
from datetime import date

from core.domain.models import Cashflow, Instrument
from core.domain import portfolio as pf


def test_position_currency():
    assert pf.position_currency("BONAR", "AL30D") == "USD"
    assert pf.position_currency("GLOBAL", "GD30D") == "USD"
    assert pf.position_currency("BONAR", "AL30") == "ARS"      # pesos (sin D)
    assert pf.position_currency("CER", "TX26") == "ARS"
    assert pf.position_currency("LECAP", "S29Y6") == "ARS"


def _metrics():
    return {
        "AL30D": {"price": 72.0, "tir": 11.0, "md": 3.8, "grupo": "Soberano",
                  "currency": "USD", "spread_curva": 0.40, "short_name": "Bonar 2030"},
        "TX26":  {"price": 100.0, "tir": 8.0, "md": 1.2, "grupo": "CER",
                  "currency": "ARS", "spread_curva": -0.20, "short_name": "Boncer 2026"},
    }


def test_build_portfolio_valuation_and_weights():
    holdings = [
        {"ticker": "AL30D", "nominal": 10000, "cost_price": 70.0},   # USD
        {"ticker": "TX26", "nominal": 1_000_000, "cost_price": 95.0},  # ARS
    ]
    out = pf.build_portfolio(holdings, _metrics(), fx_usd_ars=1200.0, as_of=date(2026, 5, 26))
    pos = {p["ticker"]: p for p in out["positions"]}

    # AL30D: 10000/100 * 72 = 7200 USD ; en ARS = 7200 * 1200 = 8,640,000
    assert pos["AL30D"]["market_value"] == 7200.0
    assert pos["AL30D"]["market_value_ars"] == 7200.0 * 1200.0
    # PnL precio: 72/70 - 1
    assert abs(pos["AL30D"]["pnl_pct"] - (72.0 / 70.0 - 1.0)) < 1e-12

    # TX26: 1,000,000/100 * 100 = 1,000,000 ARS (sin conversión)
    assert pos["TX26"]["market_value"] == 1_000_000.0
    assert pos["TX26"]["market_value_ars"] == 1_000_000.0

    s = out["summary"]
    total = 8_640_000.0 + 1_000_000.0
    assert abs(s["total_market_value_ars"] - total) < 1e-6
    # Pesos suman ~1
    assert abs(sum(p["weight"] for p in out["positions"]) - 1.0) < 1e-9
    # TIR ponderada entre 8 y 11 (AL30D pesa mucho más)
    assert 8.0 < s["weighted_tir"] < 11.0
    # MD de cartera ponderada
    w_al = 8_640_000.0 / total
    expected_md = w_al * 3.8 + (1 - w_al) * 1.2
    assert abs(s["portfolio_md"] - expected_md) < 1e-9
    # Exposición por grupo y moneda
    assert set(s["by_grupo"]) == {"Soberano", "CER"}
    assert s["by_currency"]["USD"] == 7200.0
    assert s["by_currency"]["ARS"] == 1_000_000.0


def test_build_portfolio_missing_price_is_tolerated():
    out = pf.build_portfolio(
        [{"ticker": "ZZZ", "nominal": 100, "cost_price": 50}], {}, fx_usd_ars=1000.0
    )
    p = out["positions"][0]
    assert p["price"] is None and p["market_value"] is None
    assert p["weight"] is None
    assert out["summary"]["total_market_value_ars"] == 0.0


def test_portfolio_cashflows_scales_by_nominal_and_buckets_by_month():
    inst = Instrument(
        ticker="AL30D", short_name="Bonar 2030", instrument_type="BONAR",
        maturity_date=date(2030, 7, 9),
        cashflows=[
            Cashflow(date(2026, 7, 9), amortization=0.0, interest=0.5),
            Cashflow(date(2027, 1, 9), amortization=4.0, interest=0.5),
        ],
    )
    out = pf.portfolio_cashflows(
        [{"ticker": "AL30D", "nominal": 10000}],
        {"AL30D": inst},
        today=date(2026, 5, 26),
    )
    months = {m["month"]: m for m in out["months"]}
    # units = 10000/100 = 100 → primer flujo total 0.5 * 100 = 50 USD
    assert abs(months["2026-07"]["usd"] - 50.0) < 1e-9
    assert months["2026-07"]["ars"] == 0.0
    # segundo flujo total (4.0 + 0.5) * 100 = 450 USD
    assert abs(months["2027-01"]["usd"] - 450.0) < 1e-9
    assert len(out["events"]) == 2
    assert out["events"][0]["currency"] == "USD"
