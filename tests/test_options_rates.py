"""Tests de tasas implícitas (TNA bruta, TEA bruta, TNA al strike)."""
from __future__ import annotations

import pytest

from core.domain.options.rates import compute_rates


def test_tna_bruta_basico():
    # Premio = 5% del spot, 30 días → TNA = 5% × (365/30) ≈ 60.8%
    r = compute_rates(bid=5.0, spot=100.0, strike=100.0, kind="C", t_days=30)
    assert r.tna_bruta == pytest.approx(0.05 * 365 / 30, rel=1e-6)


def test_tea_bruta_capitaliza():
    # (1.05)^(365/30) - 1 ≈ 218% TEA (yield brutal)
    r = compute_rates(bid=5.0, spot=100.0, strike=100.0, kind="C", t_days=30)
    expected = (1.05) ** (365 / 30) - 1
    assert r.tea_bruta == pytest.approx(expected, rel=1e-6)
    assert r.tea_bruta > r.tna_bruta  # TEA > TNA siempre


def test_tna_strike_call_otm():
    """Call OTM: lanzada cubierta llamada → (K + bid - S)/S anualizado."""
    # S=100, K=110, bid=3, T=30d
    # Si llamada: vendo a 110 + cobré 3 = 113; partí de 100 → 13/100 × (365/30) = 158%
    r = compute_rates(bid=3.0, spot=100.0, strike=110.0, kind="C", t_days=30)
    expected = (110 + 3 - 100) / 100 * (365 / 30)
    assert r.tna_strike == pytest.approx(expected, rel=1e-6)


def test_tna_strike_call_itm_equals_bruta():
    """Call ITM: el strike ya está adentro → tna_strike = tna_bruta."""
    r = compute_rates(bid=15.0, spot=100.0, strike=90.0, kind="C", t_days=30)
    assert r.tna_strike == pytest.approx(r.tna_bruta)


def test_tna_strike_put_otm():
    """Put OTM: cash-secured put no asignada → bid/K anualizado."""
    r = compute_rates(bid=2.0, spot=100.0, strike=95.0, kind="V", t_days=60)
    expected = (2.0 / 95.0) * (365 / 60)
    assert r.tna_strike == pytest.approx(expected, rel=1e-6)


def test_tna_strike_put_itm_can_be_negative():
    """Put ITM asignada: el bid puede no compensar el spread strike-spot."""
    # S=100, K=110 (ITM), bid=8. Asignación: pago K - bid = 102 por algo que vale 100.
    # tna_strike = (8 - 10)/100 × factor = negativo
    r = compute_rates(bid=8.0, spot=100.0, strike=110.0, kind="V", t_days=30)
    assert r.tna_strike < 0


def test_zero_bid_returns_none_when_no_last():
    """Sin bid y sin último precio operado → no hay tasa que calcular."""
    r = compute_rates(bid=0.0, spot=100.0, strike=100.0, kind="C", t_days=30, last=0.0)
    assert r.tna_bruta is None
    assert r.tea_bruta is None
    assert r.tna_strike is None


def test_zero_bid_falls_back_to_last():
    """Opciones ilíquidas (bid=0) usan el último precio operado como referencia."""
    r = compute_rates(bid=0.0, spot=100.0, strike=100.0, kind="C", t_days=30, last=5.0)
    # Tasas computadas con last=5 idénticas a si hubieras pasado bid=5.
    assert r.tna_bruta == pytest.approx(0.05 * 365 / 30, rel=1e-6)
    assert r.tea_bruta is not None and r.tea_bruta > r.tna_bruta


def test_bid_takes_priority_over_last():
    """Cuando bid > 0, last se ignora (no hay doble conteo)."""
    r = compute_rates(bid=5.0, spot=100.0, strike=100.0, kind="C", t_days=30, last=99.0)
    assert r.tna_bruta == pytest.approx(0.05 * 365 / 30, rel=1e-6)


def test_invalid_inputs_return_none():
    assert compute_rates(5, 0, 100, "C", 30).tna_bruta is None  # spot 0
    assert compute_rates(5, 100, 0, "C", 30).tna_bruta is None  # strike 0
    assert compute_rates(5, 100, 100, "C", 0).tna_bruta is None  # T=0
