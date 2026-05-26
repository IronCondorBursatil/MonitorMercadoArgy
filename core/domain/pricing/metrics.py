"""Métricas a nivel instrumento, independientes de la strategy: intereses
corridos, valor residual, current yield, DV01, convexidad, PV vanilla.

Extraído de las staticmethods de `FinancialEngine` SIN cambios de fórmula. Las
usan tanto las pricing strategies (`accrued_interest`, `vanilla_pv`) como la
fachada (popup de detalle, tab 3).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from dateutil.relativedelta import relativedelta

from core.domain.xirr import _JULIAN_YEAR


def period_bounds(instrument, ref_date: date) -> Optional[tuple]:
    """(period_start, next_cf) del cupón corriente. None si no hay cupón futuro
    con interés > 0. Maneja soberanos mid-amort cuyo Excel sólo trae flows
    futuros (infiere el período desde freq, no desde emisión)."""
    if not instrument or not instrument.cashflows:
        return None
    cfs = sorted(instrument.cashflows, key=lambda c: c.date)
    past = [c for c in cfs if c.date < ref_date]
    future = [c for c in cfs if c.date >= ref_date]
    if not future or future[0].interest <= 0:
        return None
    next_cf = future[0]
    if past:
        return past[-1].date, next_cf
    freq = getattr(instrument, "payment_frequency", 2) or 2
    months_between = max(12 // freq, 1)
    inferred_prev = next_cf.date - relativedelta(months=months_between)
    if instrument.emission_date and instrument.emission_date > inferred_prev:
        return instrument.emission_date, next_cf
    return inferred_prev, next_cf


def accrued_interest(instrument, ref_date: date) -> float:
    """Intereses corridos per-100-VN, accrual lineal sobre el cupón corriente.
    0 para zero-coupon / capitalizables (LECER, LECAP, BONCER ZC, PURO, DUAL)."""
    bounds = period_bounds(instrument, ref_date)
    if not bounds:
        return 0.0
    period_start, next_cf = bounds
    period_days = (next_cf.date - period_start).days
    if period_days <= 0:
        return 0.0
    elapsed = (ref_date - period_start).days
    if elapsed <= 0:
        return 0.0
    elapsed = min(elapsed, period_days)
    return next_cf.interest * elapsed / period_days


def days_since_last_coupon(instrument, ref_date: date) -> Optional[int]:
    """Días desde el último corte de cupón (o desde emisión si nunca pagó)."""
    bounds = period_bounds(instrument, ref_date)
    if not bounds:
        return None
    return max(0, (ref_date - bounds[0]).days)


def residual_nominal(instrument, ref_date: date) -> float:
    """VR (Valor Residual) per-100 = suma de amortizaciones futuras.
    Fallback a 100 − amortizado para schedules incompletos."""
    if not instrument or not instrument.cashflows:
        return 100.0
    past = [c for c in instrument.cashflows if c.date < ref_date]
    future = [c for c in instrument.cashflows if c.date >= ref_date]
    residual = sum(c.amortization for c in future)
    if residual <= 0:
        amortized = sum(c.amortization for c in past)
        residual = max(100.0 - amortized, 0.0)
    return residual


def current_yield(instrument, price_dirty: float, ref_date: date) -> Optional[float]:
    """Cupón anual / dirty price (decimal): intereses futuros próximos 12m / price."""
    if not instrument or not instrument.cashflows or not price_dirty or price_dirty <= 0:
        return None
    horizon = ref_date + timedelta(days=365)
    future_year_interest = sum(
        c.interest for c in instrument.cashflows
        if ref_date <= c.date <= horizon and c.interest > 0
    )
    if future_year_interest <= 0:
        return None
    return future_year_interest / price_dirty


def vanilla_pv(instrument, tir: float, ref_date: date) -> Optional[float]:
    """PV de los flujos futuros descontados al tir (act/365.25). None si no hay flujos."""
    if not instrument or tir is None or tir <= -1.0:
        return None
    future = [c for c in (instrument.cashflows or []) if c.date >= ref_date]
    if not future:
        return None
    pv = 0.0
    for cf in future:
        t = (cf.date - ref_date).days / _JULIAN_YEAR
        if t <= 0:
            continue
        pv += cf.total / (1.0 + tir) ** t
    return pv if pv > 0 else None


def dv01(instrument, tir: float, ref_date: date) -> Optional[float]:
    """Cambio de precio per-100 ante -1bp en TIR. ΔP = P(tir-1bp) − P(tir)."""
    if instrument is None or tir is None:
        return None
    p0 = vanilla_pv(instrument, tir, ref_date)
    p1 = vanilla_pv(instrument, tir - 0.0001, ref_date)
    if p0 is None or p1 is None:
        return None
    return p1 - p0


def convexity(instrument, tir: float, ref_date: date) -> Optional[float]:
    """Convexidad en años². C = (1/P) × Σ cf × t × (t+1) / (1+r)^(t+2)."""
    if instrument is None or tir is None or tir <= -1.0:
        return None
    future = [c for c in (instrument.cashflows or []) if c.date >= ref_date]
    if not future:
        return None
    pv = 0.0
    weighted = 0.0
    for cf in future:
        t = (cf.date - ref_date).days / _JULIAN_YEAR
        if t <= 0:
            continue
        pv += cf.total / (1.0 + tir) ** t
        weighted += cf.total * t * (t + 1.0) / ((1.0 + tir) ** (t + 2.0))
    if pv <= 0:
        return None
    return weighted / pv
