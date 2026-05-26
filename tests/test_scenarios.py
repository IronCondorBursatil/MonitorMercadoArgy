"""Tests del motor de escenarios (core.domain.scenarios) — puro."""
from core.domain import scenarios as sc


def test_fx_betas_by_type():
    assert sc.fx_betas("USD", "Soberano") == (0.0, 1.0)   # hard-dollar
    assert sc.fx_betas("ARS", "DL") == (1.0, 0.0)          # dolar-linked
    assert sc.fx_betas("ARS", "Dolar Linked") == (1.0, 0.0)
    assert sc.fx_betas("ARS", "CER") == (0.0, 0.0)         # pesos
    assert sc.fx_betas("ARS", "Tasa Fija") == (0.0, 0.0)


def test_price_shock_linear_and_convexity():
    # +100bps, MD 5: lineal = -5 * 0.01 = -5%
    assert abs(sc.price_shock_pct(5.0, None, 100) - (-0.05)) < 1e-12
    # con convexidad 50: -0.05 + 0.5*50*0.01^2 = -0.05 + 0.0025
    assert abs(sc.price_shock_pct(5.0, 50.0, 100) - (-0.0475)) < 1e-12
    # baja de tasa: precio sube
    assert sc.price_shock_pct(5.0, None, -100) > 0
    assert sc.price_shock_pct(None, None, 100) is None


def test_shock_usd_bond_fx_hits_ars_value_not_price():
    # Bono USD, +20% FX, sin shock de tasa: precio USD igual, valor ARS +20%
    r = sc.shock_position(price=72.0, md=3.8, currency="USD", grupo="Soberano",
                          d_tir_bps=0, d_fx_pct=20)
    assert abs(r["dp_pct"] - 0.0) < 1e-12          # precio USD no se mueve
    assert abs(r["dp_ars_pct"] - 0.20) < 1e-12     # valor ARS +20%
    assert abs(r["new_price"] - 72.0) < 1e-9       # precio USD intacto


def test_shock_dolar_linked_fx_hits_price():
    # DL cotiza en ARS y el payoff sigue al FX: +20% FX → precio ARS +20%
    r = sc.shock_position(price=100.0, md=1.0, currency="ARS", grupo="DL",
                          d_tir_bps=0, d_fx_pct=20)
    assert abs(r["dp_pct"] - 0.20) < 1e-12
    assert abs(r["dp_ars_pct"] - 0.20) < 1e-12     # ya está en el precio ARS
    assert abs(r["new_price"] - 120.0) < 1e-9


def test_shock_peso_bond_ignores_fx():
    r = sc.shock_position(price=100.0, md=2.0, currency="ARS", grupo="CER",
                          d_tir_bps=100, d_fx_pct=50)
    assert abs(r["dp_pct"] - (-0.02)) < 1e-12      # solo efecto tasa
    assert abs(r["dp_ars_pct"] - (-0.02)) < 1e-12  # FX no aplica


def test_portfolio_shock_aggregates_weighted_by_ars_value():
    positions = [
        {"ticker": "AL30D", "market_value_ars": 1000.0, "price": 72.0, "md": 3.8,
         "currency": "USD", "grupo": "Soberano"},
        {"ticker": "TX26", "market_value_ars": 1000.0, "price": 100.0, "md": 1.0,
         "currency": "ARS", "grupo": "CER"},
    ]
    # +100bps de tasa, +0% FX
    out = sc.portfolio_shock(positions, d_tir_bps=100, d_fx_pct=0)
    # AL30D: -3.8%*1000 = -38 ; TX26: -1.0%*1000 = -10 → total -48 sobre 2000
    assert abs(out["pnl_ars"] - (-48.0)) < 1e-9
    assert abs(out["pnl_pct"] - (-0.024)) < 1e-9
    assert out["base_value_ars"] == 2000.0

    # +20% FX solo: AL30D +20%*1000=+200, TX26 0 → +200
    out2 = sc.portfolio_shock(positions, d_tir_bps=0, d_fx_pct=20)
    assert abs(out2["pnl_ars"] - 200.0) < 1e-9
