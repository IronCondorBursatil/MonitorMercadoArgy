"""Tests de core/domain/yield_curve.py (M3a) — sin cobertura previa.

Curvas NS/NSS, Fisher BEI, forwards, bootstrap zero-coupon y método de pares.
Mezcla golden (valores calculados a mano) + invariantes (round-trip de fit,
repricing del bootstrap)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.domain.yield_curve import (
    NelsonSiegelCurve,
    NelsonSiegelSvenssonCurve,
    bootstrap_zero_rates,
    fisher_break_even,
    forward_bei_between_tenors,
    forward_rate,
    gamma_known_cer_factor,
    pair_delta,
    pair_monthly_inflation,
    real_fx_drift,
    _interp_rate,
)


# --- Nelson-Siegel ---------------------------------------------------------- #

def test_ns_value_at_zero_is_level_plus_slope():
    c = NelsonSiegelCurve(beta0=0.10, beta1=0.05, beta2=-0.03, gamma=1.5)
    assert c(0.0) == pytest.approx(0.15)        # β0+β1
    assert c(-1.0) == pytest.approx(0.15)       # t<=0 → mismo


def test_ns_fit_reproduces_known_curve():
    true = NelsonSiegelCurve(beta0=0.10, beta1=0.05, beta2=-0.03, gamma=1.5)
    tenors = [0.25, 0.5, 1, 2, 3, 5, 7, 10]
    yields = [true(t) for t in tenors]
    fitted = NelsonSiegelCurve.fit(tenors, yields)
    for t in tenors:
        assert fitted(t) == pytest.approx(true(t), abs=1e-4)


def test_ns_fit_rejects_too_few_points():
    with pytest.raises(ValueError):
        NelsonSiegelCurve.fit([1.0, 2.0], [0.1, 0.1])


def test_ns_fit_rejects_length_mismatch():
    with pytest.raises(ValueError):
        NelsonSiegelCurve.fit([1.0, 2.0, 3.0], [0.1, 0.1])


def test_nss_fit_reproduces_known_curve():
    true = NelsonSiegelSvenssonCurve(beta0=0.12, beta1=0.04, beta2=-0.02,
                                     beta3=0.03, tau1=0.5, tau2=4.0)
    tenors = [0.25, 0.5, 1, 1.5, 2, 3, 5, 7, 10]
    yields = [true(t) for t in tenors]
    fitted = NelsonSiegelSvenssonCurve.fit(tenors, yields)
    for t in tenors:
        assert fitted(t) == pytest.approx(true(t), abs=2e-3)


def test_nss_fit_needs_four_points():
    with pytest.raises(ValueError):
        NelsonSiegelSvenssonCurve.fit([1, 2, 3], [0.1, 0.1, 0.1])


# --- Fisher / forwards ------------------------------------------------------ #

def test_fisher_break_even_golden():
    # (1+0.30)/(1+0.10) - 1 = 0.181818...
    assert fisher_break_even(0.30, 0.10) == pytest.approx(0.1818181818, abs=1e-9)


def test_forward_of_flat_curve_equals_flat_rate():
    flat = lambda t: 0.30  # noqa: E731
    assert forward_rate(flat, 1.0, 2.0) == pytest.approx(0.30, abs=1e-9)


def test_forward_rate_invalid_interval_is_none():
    flat = lambda t: 0.30  # noqa: E731
    assert forward_rate(flat, 2.0, 1.0) is None      # t2<t1
    assert forward_rate(flat, -1.0, 1.0) is None      # t1<0


def test_forward_bei_between_tenors_composes_fisher():
    nom = lambda t: 0.30  # noqa: E731
    real = lambda t: 0.10  # noqa: E731
    # forwards de curvas planas = nivel → Fisher(0.30,0.10)
    assert forward_bei_between_tenors(nom, real, 1.0, 2.0) == pytest.approx(0.1818181818, abs=1e-9)


def test_real_fx_drift_golden_and_guards():
    assert real_fx_drift(0.50, 0.30) == pytest.approx(1.5 / 1.3 - 1, abs=1e-9)
    assert real_fx_drift(0.50, -1.0) is None         # inflación ≤ -100%
    assert real_fx_drift(None, 0.3) is None


def test_gamma_known_cer_factor():
    assert gamma_known_cer_factor(100.0, 105.0) == pytest.approx(1.05)
    assert gamma_known_cer_factor(0.0, 105.0) is None
    assert gamma_known_cer_factor(100.0, None) is None


# --- Bootstrap zero-coupon -------------------------------------------------- #

def test_bootstrap_empty_is_empty():
    assert bootstrap_zero_rates([], date(2026, 1, 1)) == []


def test_bootstrap_single_flow_reprices_bond():
    today = date(2026, 1, 1)
    mat = today + timedelta(days=365)
    bonds = [{"price": 90.0, "flows": [(mat, 100.0)], "vto": mat}]
    pts = bootstrap_zero_rates(bonds, today)
    assert len(pts) == 1
    t, spot = pts[0]
    # invariante: price·(1+spot)^t == flow
    assert 90.0 * (1.0 + spot) ** t == pytest.approx(100.0, abs=1e-6)


def test_bootstrap_orders_by_tenor():
    today = date(2026, 1, 1)
    m1 = today + timedelta(days=180)
    m2 = today + timedelta(days=540)
    bonds = [
        {"price": 95.0, "flows": [(m2, 100.0)], "vto": m2},
        {"price": 98.0, "flows": [(m1, 100.0)], "vto": m1},
    ]
    pts = bootstrap_zero_rates(bonds, today)
    assert [round(t, 3) for t, _ in pts] == sorted(round(t, 3) for t, _ in pts)


# --- Interp ----------------------------------------------------------------- #

def test_interp_rate_linear_and_flat_extrapolation():
    pts = [(1.0, 0.10), (3.0, 0.30)]
    assert _interp_rate(pts, 2.0) == pytest.approx(0.20)   # punto medio
    assert _interp_rate(pts, 0.5) == pytest.approx(0.10)   # flat izquierda
    assert _interp_rate(pts, 5.0) == pytest.approx(0.30)   # flat derecha
    assert _interp_rate([], 1.0) is None


# --- Método de pares -------------------------------------------------------- #

def test_pair_delta_guards_invalid_inputs():
    assert pair_delta(lecap_price=0, lecap_payment_at_maturity=100, cer_price=100,
                      cer_principal_plus_coupon=100, cer_base=100, cer_liq_minus_10h=100,
                      cer_last_known=100, days_to_maturity=180) is None


def test_pair_delta_neutral_scenario():
    # Con LECAP a la par (pago=precio → i=0), CER sin variación conocida (γ=1) y
    # base=liq, δ = cer_price / (principal+cupón). Si ambos = 100 → δ-1 = 0.
    d = pair_delta(lecap_price=100.0, lecap_payment_at_maturity=100.0, cer_price=100.0,
                   cer_principal_plus_coupon=100.0, cer_base=100.0, cer_liq_minus_10h=100.0,
                   cer_last_known=100.0, days_to_maturity=180)
    assert d == pytest.approx(0.0, abs=1e-9)


def test_pair_monthly_inflation_annualizes():
    # δ-1 = 0.05 sobre 30 días → mensual ≈ (1.05)^(31/30)-1
    v = pair_monthly_inflation(delta_minus_1=0.05, days_covered=30, days_per_month=31)
    assert v == pytest.approx((1.05) ** (31 / 30) - 1, abs=1e-9)
    assert pair_monthly_inflation(delta_minus_1=-1.0, days_covered=30) is None
