"""Robustez del solver de TIR (`core.domain.xirr`).

Tras la migración a Brent con auto-bracketing, el solver debe:
  - Converger en casos normales (regresión).
  - Aceptar `day_count` para descontar con la convención correcta.
  - Encontrar yields extremos (deep-discount >1000%) que el bracket viejo [-0.999,10] perdía.
  - Devolver NaN limpio (sin crash) ante entradas degeneradas / NPV sin raíz.
  - Tolerar NPV no-finito (overflow) sin propagar OverflowError.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from core.domain.daycount import DayCount
from core.domain.xirr import _xirr_from_years, xirr

TODAY = date(2026, 1, 1)


def _yr(days):
    return TODAY + timedelta(days=days)


def _npv(flows, years, rate):
    return sum(f / (1.0 + rate) ** t for f, t in zip(flows, years))


# --------------------------------------------------------------------------- #
# Regresión — casos normales que ya andaban
# --------------------------------------------------------------------------- #

def test_simple_one_year_10pct():
    tir = xirr([-100.0, 110.0], [TODAY, _yr(365)])
    assert tir == pytest.approx(0.10, abs=1e-4)


def test_par_bond_zero_yield():
    # comprar a 100, recibir 100 → yield ~0
    tir = xirr([-100.0, 100.0], [TODAY, _yr(365)])
    assert tir == pytest.approx(0.0, abs=1e-6)


def test_premium_negative_yield():
    # pagar 110, recibir 100 → yield negativo
    tir = xirr([-110.0, 100.0], [TODAY, _yr(365)])
    assert tir < 0
    assert tir > -1.0


def test_multiflow_coupon_bond():
    flows = [-100.0, 5.0, 5.0, 5.0, 105.0]
    dates = [TODAY, _yr(365), _yr(730), _yr(1095), _yr(1460)]
    tir = xirr(flows, dates)
    assert tir == pytest.approx(0.05, abs=1e-3)


# --------------------------------------------------------------------------- #
# day_count param — descuenta con la convención pedida
# --------------------------------------------------------------------------- #

def test_day_count_default_is_365_25():
    # sin day_count → años julianos (back-compat de los callers viejos)
    flows = [-95.0, 100.0]
    dates = [TODAY, _yr(365)]
    default = xirr(flows, dates)
    explicit = xirr(flows, dates, day_count=DayCount.ACT_365_25)
    assert default == pytest.approx(explicit, abs=1e-9)


def test_day_count_act365_differs_from_36525():
    flows = [-95.0, 100.0]
    dates = [TODAY, _yr(730)]
    t365 = xirr(flows, dates, day_count=DayCount.ACT_365)
    t36525 = xirr(flows, dates, day_count=DayCount.ACT_365_25)
    assert t365 != pytest.approx(t36525, abs=1e-7)
    # 365 usa menos días-año → para la misma plata, yield anual algo distinto
    assert abs(t365 - t36525) < 0.01     # diferencia chica (~bp), no enorme


def test_day_count_thirty_360():
    flows = [-95.0, 100.0]
    dates = [TODAY, date(2027, 1, 1)]
    t = xirr(flows, dates, day_count=DayCount.THIRTY_360)
    assert t > 0 and math.isfinite(t)


# --------------------------------------------------------------------------- #
# Yields extremos — el auto-bracket nuevo los encuentra
# --------------------------------------------------------------------------- #

def test_huge_yield_beyond_old_bracket():
    # pagar 1, recibir 100 en 1 año exacto (ACT/365) → yield 9900% (99.0),
    # fuera del bracket viejo [-0.999, 10] que devolvía NaN.
    tir = xirr([-1.0, 100.0], [TODAY, _yr(365)], day_count=DayCount.ACT_365)
    assert math.isfinite(tir)
    assert tir == pytest.approx(99.0, rel=1e-6)


def test_high_yield_within_range():
    # pagar 10, recibir 100 en 1 año exacto → yield 900% (9.0)
    tir = xirr([-10.0, 100.0], [TODAY, _yr(365)], day_count=DayCount.ACT_365)
    assert tir == pytest.approx(9.0, rel=1e-6)


def test_deep_discount_short_dated():
    # bono ultra-corto deep-discount: pagar 50, recibir 100 en 30 días
    tir = xirr([-50.0, 100.0], [TODAY, _yr(30)])
    assert math.isfinite(tir)
    assert tir > 1.0     # anualizado, altísimo


def test_very_negative_yield_near_total_loss():
    # recibir casi nada: pagar 100, recibir 1 en 1 año → yield ≈ -99%
    tir = xirr([-100.0, 1.0], [TODAY, _yr(365)])
    assert tir == pytest.approx(-0.99, abs=1e-3)
    assert tir > -1.0


# --------------------------------------------------------------------------- #
# Degenerados — NaN limpio, nunca excepción
# --------------------------------------------------------------------------- #

def test_single_flow_is_nan():
    assert math.isnan(xirr([-100.0], [TODAY]))


def test_empty_is_nan():
    assert math.isnan(xirr([], []))


def test_all_positive_flows_no_root():
    # sin egreso inicial → NPV nunca cruza cero
    assert math.isnan(xirr([100.0, 100.0], [TODAY, _yr(365)]))


def test_all_negative_flows_no_root():
    assert math.isnan(xirr([-100.0, -100.0], [TODAY, _yr(365)]))


def test_zero_flows_is_nan():
    assert math.isnan(xirr([0.0, 0.0], [TODAY, _yr(365)]))


# --------------------------------------------------------------------------- #
# _xirr_from_years — capa interna, tolerante a overflow
# --------------------------------------------------------------------------- #

def test_xirr_from_years_basic():
    flows = np.array([-100.0, 110.0])
    years = np.array([0.0, 1.0])
    assert _xirr_from_years(flows, years) == pytest.approx(0.10, abs=1e-6)


def test_xirr_from_years_overflow_returns_nan_not_crash():
    # años enormes + flujo que fuerza (1+r)^t a overflow → NaN limpio, sin OverflowError
    flows = np.array([-1e-9, 100.0])
    years = np.array([0.0, 200.0])
    res = _xirr_from_years(flows, years)
    assert math.isnan(res) or math.isfinite(res)   # nunca tira excepción


def test_xirr_from_years_accepts_day_count_kwarg():
    # firma compat: acepta day_count (lo ignora; los years ya vienen calculados)
    flows = np.array([-100.0, 110.0])
    years = np.array([0.0, 1.0])
    assert _xirr_from_years(flows, years, day_count=None) == pytest.approx(0.10, abs=1e-6)


def test_no_overflow_error_propagates():
    # caso CUAP-like: yield absurdo no debe crashear el solver
    flows = [-0.04, 5.0, 5.0, 105.0]
    dates = [TODAY, _yr(365), _yr(730), _yr(6935)]
    try:
        res = xirr(flows, dates)
    except OverflowError:
        pytest.fail("el solver propagó OverflowError en vez de manejarlo")
    assert math.isnan(res) or math.isfinite(res)


# --------------------------------------------------------------------------- #
# Recuperación de yield conocido — round-trip a través del solver (grid amplio)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("true_yield", [
    -0.50, -0.30, -0.15, -0.05, 0.0, 0.03, 0.07, 0.12, 0.20, 0.35,
    0.55, 0.85, 1.50, 3.0, 9.0, 25.0, 80.0,
])
@pytest.mark.parametrize("n", [2, 3, 5, 8])
@pytest.mark.parametrize("conv", list(DayCount))
def test_recover_known_yield(true_yield, n, conv):
    from core.domain.daycount import year_fraction
    # Cupones de 5 + 100 al final, en años 1..n. Precio = PV a true_yield bajo
    # `conv`; el solver debe recuperar true_yield descontando con la misma conv.
    dates = [_yr(365 * i) for i in range(0, n + 1)]
    cfs = [5.0] * n
    cfs[-1] += 100.0
    yfs = [year_fraction(TODAY, d, conv) for d in dates[1:]]
    price = sum(cf / (1.0 + true_yield) ** t for cf, t in zip(cfs, yfs))
    flows = [-price] + cfs
    got = xirr(flows, dates, day_count=conv)
    assert got == pytest.approx(true_yield, abs=1e-6)


@pytest.mark.parametrize("days_ahead", [1, 7, 30, 91, 182, 365, 400, 1000, 3650])
def test_bullet_yield_recovery_default_conv(days_ahead):
    # bullet -P, 100; con yield 25% recupera exacto sea cual sea el plazo
    from core.domain.daycount import year_fraction
    r = 0.25
    mat = _yr(days_ahead)
    t = year_fraction(TODAY, mat, DayCount.ACT_365_25)
    price = 100.0 / (1.0 + r) ** t
    assert xirr([-price, 100.0], [TODAY, mat]) == pytest.approx(r, abs=1e-7)
