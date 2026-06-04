"""Invariantes de pricing — property-based (hypothesis) + grids parametrizados.

No dependen de valores de mercado: verifican PROPIEDADES matemáticas que deben
valer para cualquier bono vanilla y cualquier convención de día-count:
  - Round-trip: price → TIR → price_from_tir(TIR) ≈ price.
  - Monotonicidad: price↓ ⇒ TIR↑ (a precios de descuento crecientes).
  - Cotas de duración: 0 < MD < Macaulay < años-a-vto; convexidad > 0; DV01 > 0.
  - Solver: entradas válidas → TIR finita o NaN, nunca excepción.

Esto blinda el motor ante regresiones de signo/cota que los golden puntuales no
cubren, sobre todo en los caminos de día-count nuevos.
"""

from __future__ import annotations

import math
from datetime import date

import pytest
from dateutil.relativedelta import relativedelta
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from core.domain.daycount import DayCount, year_fraction
from core.domain.models import Cashflow, Instrument
from core.domain.pricing import metrics
from core.domain.pricing.base import VanillaStrategy
from core.domain.pricing.context import PricingContext

SETTLE = date(2026, 6, 1)
_STRAT = VanillaStrategy()
_CONV_STR = {
    DayCount.ACT_365: "ACT/365", DayCount.ACT_365_25: "ACT/365.25",
    DayCount.THIRTY_360: "30/360", DayCount.ACT_ACT: "ACT/ACT",
}


def _bullet(coupon_pct, n_years, freq, conv, ticker="P"):
    """Bullet con cupones regulares desde SETTLE a vto = SETTLE + n_years."""
    maturity = SETTLE + relativedelta(years=n_years)
    emission = SETTLE - relativedelta(years=2)
    months = max(12 // freq, 1)
    coupon = coupon_pct / 100.0 / freq * 100.0  # monto per-100 por cupón
    cfs = []
    d = maturity
    dates = []
    while d > SETTLE:
        dates.append(d)
        d = d - relativedelta(months=months)
    for dt in sorted(dates):
        amort = 100.0 if dt == maturity else 0.0
        cfs.append(Cashflow(date=dt, amortization=amort, interest=coupon))
    return Instrument(
        ticker=ticker, short_name=ticker, instrument_type="BONAR",
        maturity_date=maturity, emission_date=emission,
        payment_frequency=freq, day_count=_CONV_STR[conv], cashflows=tuple(cfs),
    )


def _tir(inst, price):
    return _STRAT.tir(inst, price, PricingContext(settle=SETTLE))


def _pv(inst, tir):
    return _STRAT.price_from_tir(inst, tir, PricingContext(settle=SETTLE))


def _dur(inst, tir):
    return _STRAT.duration(inst, tir, PricingContext(settle=SETTLE))


_coupons = st.floats(min_value=0.0, max_value=15.0)
_years = st.integers(min_value=1, max_value=30)
_freqs = st.sampled_from([1, 2, 4])
_convs = st.sampled_from(list(DayCount))
_prices = st.floats(min_value=40.0, max_value=140.0)

_HSET = settings(max_examples=120, deadline=None)


# --------------------------------------------------------------------------- #
# Round-trip price → TIR → price
# --------------------------------------------------------------------------- #

@_HSET
@given(coupon=_coupons, n_years=_years, freq=_freqs, conv=_convs, price=_prices)
def test_roundtrip_price_tir_price(coupon, n_years, freq, conv, price):
    inst = _bullet(coupon, n_years, freq, conv)
    tir = _tir(inst, price)
    assume(tir is not None and math.isfinite(tir) and tir > -0.99)
    back = _pv(inst, tir)
    assume(back is not None)
    assert back == pytest.approx(price, rel=1e-6, abs=1e-6)


@pytest.mark.parametrize("conv", list(DayCount))
@pytest.mark.parametrize("price", [55.0, 70.0, 85.0, 95.0, 100.0, 110.0, 125.0])
@pytest.mark.parametrize("coupon,n_years,freq", [
    (0.0, 5, 1), (5.0, 3, 2), (8.0, 10, 2), (3.5, 7, 4), (12.0, 2, 2),
])
def test_roundtrip_grid(conv, price, coupon, n_years, freq):
    inst = _bullet(coupon, n_years, freq, conv)
    tir = _tir(inst, price)
    assert tir is not None and math.isfinite(tir)
    back = _pv(inst, tir)
    assert back == pytest.approx(price, rel=1e-6, abs=1e-6)


# --------------------------------------------------------------------------- #
# Monotonicidad: precio más bajo ⇒ TIR más alta
# --------------------------------------------------------------------------- #

@_HSET
@given(coupon=_coupons, n_years=_years, freq=_freqs, conv=_convs)
def test_lower_price_higher_tir(coupon, n_years, freq, conv):
    inst = _bullet(coupon, n_years, freq, conv)
    t_low = _tir(inst, 80.0)
    t_high = _tir(inst, 100.0)
    assume(t_low is not None and t_high is not None)
    assert t_low > t_high     # comprar más barato → mayor rendimiento


@pytest.mark.parametrize("conv", list(DayCount))
@pytest.mark.parametrize("p_lo,p_hi", [(60, 80), (80, 100), (90, 110), (100, 130)])
def test_monotonic_grid(conv, p_lo, p_hi):
    inst = _bullet(6.0, 8, 2, conv)
    assert _tir(inst, float(p_lo)) > _tir(inst, float(p_hi))


# --------------------------------------------------------------------------- #
# Cotas de duración y convexidad
# --------------------------------------------------------------------------- #

@_HSET
@given(coupon=_coupons, n_years=_years, freq=_freqs, conv=_convs, price=_prices)
def test_duration_bounds(coupon, n_years, freq, conv, price):
    inst = _bullet(coupon, n_years, freq, conv)
    tir = _tir(inst, price)
    assume(tir is not None and tir > -0.5)
    md = _dur(inst, tir)
    assume(md is not None)
    yf_mat = year_fraction(SETTLE, inst.maturity_date, conv)
    assert md > 0
    # MD = Macaulay/(1+tir)^(1/m): ≤ Macaulay ≤ años-a-vto SÓLO si tir≥0.
    # Con yield negativo (bono a premium) MD puede exceder la Macaulay.
    if tir >= 0:
        assert md <= yf_mat + 1e-9


@_HSET
@given(coupon=_coupons, n_years=_years, freq=_freqs, conv=_convs, price=_prices)
def test_convexity_positive(coupon, n_years, freq, conv, price):
    inst = _bullet(coupon, n_years, freq, conv)
    tir = _tir(inst, price)
    assume(tir is not None and tir > -0.5)
    cx = metrics.convexity(inst, tir, SETTLE)
    assume(cx is not None)
    assert cx > 0


@_HSET
@given(coupon=_coupons, n_years=_years, freq=_freqs, conv=_convs, price=_prices)
def test_dv01_positive(coupon, n_years, freq, conv, price):
    # DV01 = P(tir-1bp) - P(tir) > 0 (el precio sube cuando el yield baja)
    inst = _bullet(coupon, n_years, freq, conv)
    tir = _tir(inst, price)
    assume(tir is not None and tir > -0.5)
    d = metrics.dv01(inst, tir, SETTLE)
    assume(d is not None)
    assert d > 0


@pytest.mark.parametrize("conv", list(DayCount))
@pytest.mark.parametrize("n_years", [1, 3, 5, 10, 20])
def test_zero_coupon_macaulay_equals_maturity(conv, n_years):
    # ZC: Macaulay == años-a-vto (un solo flujo). MD = Macaulay/(1+tir) < Macaulay.
    inst = _bullet(0.0, n_years, 1, conv)
    tir = _tir(inst, 75.0)
    md = _dur(inst, tir)
    yf = year_fraction(SETTLE, inst.maturity_date, conv)
    assert md == pytest.approx(yf / (1 + tir), rel=1e-7)
    assert md < yf


# --------------------------------------------------------------------------- #
# Solver: nunca crashea, devuelve finito o NaN
# --------------------------------------------------------------------------- #

@_HSET
@given(
    flows=st.lists(st.floats(min_value=-200, max_value=200, allow_nan=False,
                             allow_infinity=False), min_size=2, max_size=12),
    base_days=st.integers(min_value=10, max_value=12000),
)
def test_solver_never_crashes(flows, base_days):
    from core.domain.xirr import xirr
    dates = [SETTLE + relativedelta(days=int(base_days) * i) for i in range(len(flows))]
    try:
        res = xirr(flows, dates)
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"xirr lanzó {type(e).__name__}: {e}")
    assert res is not None
    assert math.isnan(res) or math.isfinite(res)


@_HSET
@given(coupon=_coupons, n_years=_years, freq=_freqs, conv=_convs, price=_prices)
def test_tir_finite_or_none(coupon, n_years, freq, conv, price):
    inst = _bullet(coupon, n_years, freq, conv)
    tir = _tir(inst, price)
    assert tir is None or math.isfinite(tir)
