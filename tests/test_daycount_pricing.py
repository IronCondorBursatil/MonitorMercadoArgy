"""Pricing por convención de día-count — el corazón del bug arreglado.

Verifica que TIR/duration/PV descuentan con la convención DECLARADA del bono
(no el viejo 365.25 cableado):
  - ZC bullet: TIR == forma cerrada (payoff/price)^(1/yf) − 1 por convención.
  - Gap-lock: ACT/365.25 da TIR mayor que ACT/365 (el ~1bp que separaba de Balanz).
  - 30/360 consistente: TIR y PV usan 30/360 (antes PV caía a 365.25 → round-trip roto).
  - Accrued por convención (30/360 vs ACT).
  - Bonos raros/extremos: ZC ultra-largo, 1-día, Feb-29, step-up, amortizing,
    par/premium/deep-discount → sin crash + cotas sanas.

Se prueba contra `VanillaStrategy` directo (settle exacto, sin resolve_settle) para
poder afirmar formas cerradas, y contra el facade para el smoke de integración.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.domain.daycount import DayCount, year_fraction
from core.domain.models import Cashflow, Instrument, MarketSnapshot
from core.domain.pricing.base import VanillaStrategy
from core.domain.pricing.context import PricingContext
from core.domain.services import FinancialEngine

SETTLE = date(2026, 6, 1)
_STRAT = VanillaStrategy()
_DC_STR = {
    DayCount.ACT_365: "ACT/365",
    DayCount.ACT_365_25: "ACT/365.25",
    DayCount.THIRTY_360: "30/360",
    DayCount.ACT_ACT: "ACT/ACT",
}


def _zc(day_count_str, maturity, emission=None):
    """Zero-coupon bullet (un flujo de 100 al vto)."""
    return Instrument(
        ticker="ZC", short_name="ZC", instrument_type="BONAR",
        maturity_date=maturity, emission_date=emission or (maturity - timedelta(days=365)),
        payment_frequency=1, day_count=day_count_str,
        cashflows=(Cashflow(date=maturity, amortization=100.0, interest=0.0),),
    )


def _coupon_bond(day_count_str, maturity, emission, coupon_dates, coupon_amt, freq=2):
    """Bullet con cupones (interés en cada coupon_date, amort 100 al vto)."""
    cfs = []
    for d in coupon_dates:
        amort = 100.0 if d == maturity else 0.0
        cfs.append(Cashflow(date=d, amortization=amort, interest=coupon_amt))
    if maturity not in coupon_dates:
        cfs.append(Cashflow(date=maturity, amortization=100.0, interest=coupon_amt))
    return Instrument(
        ticker="CB", short_name="CB", instrument_type="BONAR",
        maturity_date=maturity, emission_date=emission,
        payment_frequency=freq, day_count=day_count_str, cashflows=tuple(sorted(cfs, key=lambda c: c.date)),
    )


def _tir(inst, price, settle=SETTLE):
    return _STRAT.tir(inst, price, PricingContext(settle=settle))


def _dur(inst, tir, settle=SETTLE):
    return _STRAT.duration(inst, tir, PricingContext(settle=settle))


# --------------------------------------------------------------------------- #
# Invariante del modelo: cashflows cronológicos (el pricing lo asume)
# --------------------------------------------------------------------------- #

def test_instrument_sorts_cashflows_chronologically():
    out_of_order = (
        Cashflow(date=date(2028, 6, 1), amortization=100.0, interest=4.0),
        Cashflow(date=date(2026, 6, 1), amortization=0.0, interest=4.0),
        Cashflow(date=date(2027, 6, 1), amortization=0.0, interest=4.0),
    )
    inst = Instrument(ticker="X", short_name="X", instrument_type="BONAR",
                      maturity_date=date(2028, 6, 1), day_count="ACT/365",
                      cashflows=out_of_order)
    dates = [cf.date for cf in inst.cashflows]
    assert dates == sorted(dates)
    assert dates[0] == date(2026, 6, 1) and dates[-1] == date(2028, 6, 1)


@pytest.mark.parametrize("stub_frac,should_extend", [
    (0.05, False),   # cola diminuta (artefacto tipo AO28) → no extender
    (0.11, True),    # stub corto genuino
    (0.50, True),
    (0.678, True),   # CLISA-like
    (0.95, False),   # casi período completo → no tocar
    (1.50, False),   # long-last-coupon → no tocar
])
def test_stub_extension_threshold(stub_frac, should_extend):
    from datetime import timedelta
    from core.domain.pricing.metrics import discount_year_fractions
    settle = date(2026, 1, 1)
    c1 = date(2026, 6, 30)                                   # cupón regular
    final = c1 + timedelta(days=round(stub_frac * 180))      # flujo final a stub_frac del período
    cfs = (Cashflow(date=c1, interest=3.0, amortization=0.0),
           Cashflow(date=final, interest=3.0, amortization=100.0))
    inst = Instrument(ticker="T", short_name="T", instrument_type="BONAR",
                      maturity_date=final, payment_frequency=2, day_count="30/360", cashflows=cfs)
    _, years = discount_year_fractions(inst, settle)
    extended = abs(years[-1] - inst.year_fraction_to(final, settle)) > 1e-9
    assert extended is should_extend


def test_stub_not_extended_when_prev_is_amort_only():
    # cfs[-2] amort-only (interest=0) NO marca un período regular → no extender (guard)
    from core.domain.pricing.metrics import discount_year_fractions
    settle = date(2026, 1, 1)
    cfs = (Cashflow(date=date(2026, 6, 30), interest=0.0, amortization=20.0),  # amort-only
           Cashflow(date=date(2026, 9, 28), interest=3.0, amortization=80.0))  # stub ~0.5
    inst = Instrument(ticker="T", short_name="T", instrument_type="BONAR",
                      maturity_date=date(2026, 9, 28), payment_frequency=2,
                      day_count="30/360", cashflows=cfs)
    _, years = discount_year_fractions(inst, settle)
    assert years[-1] == pytest.approx(inst.year_fraction_to(date(2026, 9, 28), settle))


def test_sort_is_stable_for_same_date_flows():
    # mismo día (cupón + amort): el sort estable preserva el orden de inserción
    same_day = (
        Cashflow(date=date(2027, 6, 1), amortization=0.0, interest=3.0),
        Cashflow(date=date(2027, 6, 1), amortization=100.0, interest=0.0),
    )
    inst = Instrument(ticker="Y", short_name="Y", instrument_type="BONAR",
                      maturity_date=date(2027, 6, 1), cashflows=same_day)
    assert inst.cashflows[0].interest == 3.0
    assert inst.cashflows[1].amortization == 100.0


# --------------------------------------------------------------------------- #
# ZC bullet: TIR == forma cerrada por convención
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("conv", list(DayCount))
@pytest.mark.parametrize("price,years_ahead", [
    (95.0, 1), (90.0, 2), (80.0, 3), (70.0, 5), (50.0, 7), (99.0, 1), (105.0, 2),
])
def test_zc_tir_matches_closed_form(conv, price, years_ahead):
    maturity = date(SETTLE.year + years_ahead, SETTLE.month, SETTLE.day)
    inst = _zc(_DC_STR[conv], maturity)
    yf = year_fraction(SETTLE, maturity, conv)
    expected = (100.0 / price) ** (1.0 / yf) - 1.0
    got = _tir(inst, price)
    assert got == pytest.approx(expected, abs=1e-7)


# --------------------------------------------------------------------------- #
# Gap-lock: ACT/365.25 > ACT/365 (el ~1bp que separaba de Balanz)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("price,years_ahead", [
    (95.0, 1), (90.0, 2), (85.0, 3), (75.0, 5), (60.0, 8), (98.0, 2), (88.0, 4),
])
def test_act365_lower_than_36525_for_discount(price, years_ahead):
    # SÓLO bonos a descuento (price<100): ahí 365.25 da TIR mayor. En premium
    # (price>100) la relación se invierte (TIR negativa) — cubierto por la
    # identidad de factores de capitalización en el test de abajo.
    assert price < 100.0
    maturity = date(SETTLE.year + years_ahead, SETTLE.month, SETTLE.day)
    t365 = _tir(_zc("ACT/365", maturity), price)
    t36525 = _tir(_zc("ACT/365.25", maturity), price)
    # 365.25 usa más año-fracción → TIR algo mayor (para bono a descuento).
    assert t36525 > t365
    # el gap es chico (~bp), del orden de 365.25/365 − 1 ≈ 0.07%
    assert (t36525 - t365) / t365 < 0.01


@pytest.mark.parametrize("years_ahead", [1, 2, 3, 5, 10])
def test_gap_ratio_near_36525_over_365_for_zc(years_ahead):
    # Para ZC, tir = ratio^(1/yf) − 1. Con yf365 = d/365, yf36525 = d/365.25.
    # En el límite chico, (1+tir36525) ≈ (1+tir365)^(365/365.25). Chequeamos
    # la identidad exacta de los factores de capitalización.
    maturity = date(SETTLE.year + years_ahead, SETTLE.month, SETTLE.day)
    price = 80.0
    t365 = _tir(_zc("ACT/365", maturity), price)
    t36525 = _tir(_zc("ACT/365.25", maturity), price)
    # (1+t365)^(d/365) == (1+t36525)^(d/365.25) == 100/price (ambos descuentan igual)
    yf365 = year_fraction(SETTLE, maturity, DayCount.ACT_365)
    yf36525 = year_fraction(SETTLE, maturity, DayCount.ACT_365_25)
    assert (1 + t365) ** yf365 == pytest.approx((1 + t36525) ** yf36525, rel=1e-9)


# --------------------------------------------------------------------------- #
# 30/360 consistente: round-trip precio → TIR → precio (antes roto: PV a 365.25)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("conv", list(DayCount))
@pytest.mark.parametrize("price", [85.0, 95.0, 100.0, 105.0])
def test_price_tir_price_roundtrip(conv, price):
    maturity = date(2031, 6, 1)
    emission = date(2026, 1, 1)
    coupons = [date(y, m, 1) for y in range(2026, 2032) for m in (6, 12)
               if date(y, m, 1) > SETTLE and date(y, m, 1) <= maturity]
    inst = _coupon_bond(_DC_STR[conv], maturity, emission, coupons, coupon_amt=3.0, freq=2)
    tir = _tir(inst, price)
    assert tir is not None
    back = _STRAT.price_from_tir(inst, tir, PricingContext(settle=SETTLE))
    assert back == pytest.approx(price, rel=1e-6)


# --------------------------------------------------------------------------- #
# Accrued por convención
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("conv_str", ["ACT/365", "ACT/365.25", "30/360"])
def test_accrued_zero_for_zero_coupon(conv_str):
    from core.domain.pricing.metrics import accrued_interest
    inst = _zc(conv_str, date(2030, 6, 1))
    assert accrued_interest(inst, SETTLE) == 0.0


@pytest.mark.parametrize("conv_str", ["ACT/365", "30/360"])
def test_accrued_grows_with_elapsed(conv_str):
    from core.domain.pricing.metrics import accrued_interest
    maturity = date(2030, 1, 1)
    emission = date(2025, 1, 1)
    coupons = [date(y, m, 1) for y in range(2025, 2031) for m in (1, 7)
               if date(y, m, 1) > emission and date(y, m, 1) <= maturity]
    inst = _coupon_bond(conv_str, maturity, emission, coupons, coupon_amt=4.0, freq=2)
    # accrued en distintos puntos de un período (cupones 1-Ene / 1-Jul)
    a_early = accrued_interest(inst, date(2027, 7, 15))
    a_mid = accrued_interest(inst, date(2027, 9, 1))
    a_late = accrued_interest(inst, date(2027, 12, 20))
    assert 0 < a_early < a_mid < a_late


def test_accrued_30_360_vs_act_differ():
    from core.domain.pricing.metrics import accrued_interest
    maturity = date(2030, 1, 1)
    emission = date(2025, 1, 1)
    coupons = [date(y, m, 1) for y in range(2025, 2031) for m in (1, 7)
               if date(y, m, 1) > emission and date(y, m, 1) <= maturity]
    ref = date(2027, 8, 17)
    a_act = accrued_interest(_coupon_bond("ACT/365", maturity, emission, coupons, 4.0), ref)
    a_360 = accrued_interest(_coupon_bond("30/360", maturity, emission, coupons, 4.0), ref)
    assert a_act > 0 and a_360 > 0
    assert a_act != pytest.approx(a_360, abs=1e-4)


# --------------------------------------------------------------------------- #
# Duration: cotas y signo, por convención
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("conv", list(DayCount))
@pytest.mark.parametrize("years_ahead", [1, 2, 5, 10])
def test_zc_duration_close_to_maturity(conv, years_ahead):
    maturity = date(SETTLE.year + years_ahead, SETTLE.month, SETTLE.day)
    inst = _zc(_DC_STR[conv], maturity)
    tir = _tir(inst, 90.0)
    md = _dur(inst, tir)
    yf = year_fraction(SETTLE, maturity, conv)
    # ZC: Macaulay == yf; MD = yf/(1+tir)^(1/1) (freq=1 single flow) < yf
    assert md is not None
    assert 0 < md < yf
    assert md == pytest.approx(yf / (1 + tir), abs=1e-7)


@pytest.mark.parametrize("conv", list(DayCount))
def test_coupon_duration_positive_and_below_maturity(conv):
    maturity = date(2033, 6, 1)
    coupons = [date(y, m, 1) for y in range(2026, 2034) for m in (6, 12)
               if date(y, m, 1) > SETTLE and date(y, m, 1) <= maturity]
    inst = _coupon_bond(_DC_STR[conv], maturity, date(2024, 6, 1), coupons, 3.5, 2)
    tir = _tir(inst, 95.0)
    md = _dur(inst, tir)
    assert md is not None and md > 0
    assert md < year_fraction(SETTLE, maturity, conv)


# --------------------------------------------------------------------------- #
# Bonos raros / extremos — sin crash + cotas
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("conv", list(DayCount))
def test_ultra_long_zero_coupon_50y(conv):
    inst = _zc(_DC_STR[conv], date(2076, 6, 1))
    tir = _tir(inst, 5.0)        # deep discount, 50 años
    assert tir is not None and tir > 0
    md = _dur(inst, tir)
    assert md is not None and md > 0


@pytest.mark.parametrize("conv", list(DayCount))
def test_one_day_to_maturity(conv):
    inst = _zc(_DC_STR[conv], SETTLE + timedelta(days=1))
    tir = _tir(inst, 99.5)
    assert tir is not None
    import math
    assert math.isfinite(tir)


@pytest.mark.parametrize("conv", list(DayCount))
def test_feb29_maturity(conv):
    # vto en Feb-29 de un año bisiesto
    inst = _zc(_DC_STR[conv], date(2028, 2, 29), emission=date(2026, 2, 28))
    tir = _tir(inst, 88.0)
    assert tir is not None and tir > 0


@pytest.mark.parametrize("conv", list(DayCount))
def test_step_up_coupon_schedule(conv):
    # cupones crecientes (step-up) modelados como interés distinto por fecha
    maturity = date(2031, 6, 1)
    dates_amts = [(date(2027, 6, 1), 2.0), (date(2028, 6, 1), 3.0),
                  (date(2029, 6, 1), 4.0), (date(2030, 6, 1), 5.0),
                  (date(2031, 6, 1), 6.0)]
    cfs = [Cashflow(date=d, amortization=(100.0 if d == maturity else 0.0), interest=i)
           for d, i in dates_amts]
    inst = Instrument(ticker="SU", short_name="SU", instrument_type="BONAR",
                      maturity_date=maturity, emission_date=date(2026, 6, 1),
                      payment_frequency=1, day_count=_DC_STR[conv], cashflows=tuple(cfs))
    tir = _tir(inst, 97.0)
    assert tir is not None and tir > 0


@pytest.mark.parametrize("conv", list(DayCount))
def test_amortizing_schedule(conv):
    # amortizing: capital en cuotas, cupón sobre saldo decreciente
    maturity = date(2030, 6, 1)
    sched = [date(2028, 6, 1), date(2029, 6, 1), date(2030, 6, 1)]
    cfs = [Cashflow(date=d, amortization=100.0 / 3, interest=2.0) for d in sched]
    inst = Instrument(ticker="AM", short_name="AM", instrument_type="BONAR",
                      maturity_date=maturity, emission_date=date(2026, 6, 1),
                      payment_frequency=1, day_count=_DC_STR[conv], cashflows=tuple(cfs))
    tir = _tir(inst, 95.0)
    assert tir is not None
    md = _dur(inst, tir)
    assert md is not None and md > 0


@pytest.mark.parametrize("conv", list(DayCount))
@pytest.mark.parametrize("price,sign", [(80.0, 1), (100.0, 0), (120.0, -1)])
def test_par_premium_discount_tir_sign(conv, price, sign):
    # bono con cupón ~ coincide con yield a la par; a descuento TIR>cupón, a premium TIR<cupón
    maturity = date(2031, 6, 1)
    coupons = [date(y, m, 1) for y in range(2026, 2032) for m in (6, 12)
               if date(y, m, 1) > SETTLE and date(y, m, 1) <= maturity]
    inst = _coupon_bond(_DC_STR[conv], maturity, date(2024, 6, 1), coupons, 3.0, 2)
    tir = _tir(inst, price)
    assert tir is not None
    if sign > 0:
        assert tir > 0.06          # descuento → yield alto
    elif sign < 0:
        assert tir < 0.06          # premium → yield bajo


# --------------------------------------------------------------------------- #
# Integración por el facade (con resolve_settle real)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("conv_str", ["ACT/365", "ACT/365.25", "30/360", "ACT/ACT"])
def test_facade_calculate_tir_smoke(conv_str):
    inst = _zc(conv_str, date(2030, 6, 1))
    snap = MarketSnapshot(instrument=inst, price=90.0, last_update=SETTLE)
    tir = FinancialEngine.calculate_tir(snap, indices_provider=None, settle_date=SETTLE)
    assert tir is not None and tir > 0
    md = FinancialEngine.calculate_duration(snap, tir, settle_date=SETTLE)
    assert md is not None and md > 0
