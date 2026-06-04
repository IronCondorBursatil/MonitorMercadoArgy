"""Tests del generador de cashflows USD para Obligaciones Negociables.

ONs hard-dollar (ticker …D, cotiza en USD): cupón fijo semestral/trimestral/anual,
amortización bullet o en cuotas (cadencia de capital propia, distinta del cupón),
o cupón cero. Day-count 30/360 (USD corporate, convención IAMC)."""

from datetime import date

from core.domain.on_cashflows import amort_schedule, build_on_cashflows


def _by_date(cfs):
    return {cf.date: cf for cf in cfs}


def test_bullet_semestral_coupons_and_principal_at_maturity():
    # MGCRO/MGCRD: emis 2025-11-14, vto 2037-11-14, cupón 7.8% semestral, VR 100, bullet.
    cfs = build_on_cashflows(
        emission=date(2025, 11, 14), maturity=date(2037, 11, 14),
        coupon_rate=0.078, coupon_freq=2, vr=100.0,
    )
    last = cfs[-1]
    assert last.date == date(2037, 11, 14)
    assert round(last.amortization, 6) == 100.0           # principal bullet al vto
    assert round(last.interest, 4) == round(100 * 0.078 / 2, 4)  # cupón semestral 3.9
    # todo el capital se paga 1 sola vez
    assert round(sum(cf.amortization for cf in cfs), 6) == 100.0
    # cupones cada ~6 meses, todos del mismo monto (bullet → outstanding constante)
    coupons = [cf for cf in cfs if cf.interest > 0]
    assert len(coupons) >= 23  # ~24 cupones en 12 años
    assert all(round(cf.interest, 6) == round(3.9, 6) for cf in coupons)
    assert all(cf.date > date(2025, 11, 14) for cf in cfs)


def test_amortizing_declining_principal_with_capital_cadence():
    # 3 cuotas anuales de capital terminando al vto; cupón semestral sobre el saldo.
    sched = amort_schedule(date(2031, 1, 15), date(2033, 1, 15), capital_freq=1, cuotas=3)
    assert sched == [date(2031, 1, 15), date(2032, 1, 15), date(2033, 1, 15)]
    cfs = build_on_cashflows(
        emission=date(2030, 1, 15), maturity=date(2033, 1, 15),
        coupon_rate=0.10, coupon_freq=2, vr=99.0, amort_dates=sched,
    )
    amorts = sorted(cf.date for cf in cfs if cf.amortization > 0)
    assert amorts == [date(2031, 1, 15), date(2032, 1, 15), date(2033, 1, 15)]
    assert round(sum(cf.amortization for cf in cfs), 6) == 99.0       # suma = VR
    by = _by_date(cfs)
    # cupón sobre saldo decreciente: el cupón del último año < el del primero
    first_amort_coupon = by[date(2031, 1, 15)].interest   # saldo 99 → 99*.10/2 = 4.95
    last_coupon = by[date(2033, 1, 15)].interest          # saldo 33 → 33*.10/2 = 1.65
    assert round(first_amort_coupon, 4) == 4.95
    assert round(last_coupon, 4) == 1.65


def test_zero_coupon_single_payoff():
    cfs = build_on_cashflows(
        emission=date(2024, 7, 5), maturity=date(2027, 7, 5),
        coupon_rate=0.0, coupon_freq=1, vr=100.0, zero_coupon=True,
    )
    assert len(cfs) == 1
    assert cfs[0].date == date(2027, 7, 5)
    assert cfs[0].amortization == 100.0 and cfs[0].interest == 0.0


def test_partial_residual_bullet_keeps_vr_base():
    # VR < 100 (ya amortizó parte): bullet del residual al vencimiento.
    cfs = build_on_cashflows(
        emission=date(2021, 4, 30), maturity=date(2027, 4, 30),
        coupon_rate=0.091, coupon_freq=2, vr=40.0,
    )
    assert round(sum(cf.amortization for cf in cfs), 6) == 40.0
    coupons = [cf for cf in cfs if cf.interest > 0]
    assert all(round(cf.interest, 6) == round(40 * 0.091 / 2, 6) for cf in coupons)
