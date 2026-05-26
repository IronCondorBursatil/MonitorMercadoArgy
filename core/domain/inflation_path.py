"""Senderos de inflación implícita por mes calendario.

Implementa la Fig. 4 de NT N°8/2024 (Matarrelli & Pastore, Dic 2024):
para cada mes M en los próximos N meses, computa la BEI implícita usando
el forward Fisher entre el inicio y el fin del mes contra las curvas
nominal y real estimadas (NSS o NS).

Salida típica:
    [{"label": "may-26", "monthly_bei": 0.025, "annualized_bei": 0.345, ...}, ...]
"""

import calendar
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from core.domain.yield_curve import forward_rate, fisher_break_even


@dataclass(frozen=True)
class MonthlyBEI:
    label: str           # e.g. "may-26"
    month_start: date    # first day of the month
    month_end: date      # last day of the month
    days_in_month: int
    monthly_bei: Optional[float]      # decimal, e.g. 0.025 = 2.5%
    annualized_bei: Optional[float]   # compounded TEA equivalent
    forward_nominal: Optional[float]  # forward nominal rate for that interval (TEA)
    forward_real: Optional[float]


_MONTH_ES = ["ene", "feb", "mar", "abr", "may", "jun",
             "jul", "ago", "sep", "oct", "nov", "dic"]


def _next_month_start(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def monthly_inflation_path(
    nom_curve,
    real_curve,
    today: date,
    months_ahead: int = 12,
    min_segment_days: int = 5,
) -> List[MonthlyBEI]:
    """For each upcoming calendar month, compute the BEI forward between
    today and that month's boundaries, then back out the monthly rate.

    The first month is partial (from `today` to its month-end), so the
    monthly BEI for it is interpolated from the partial-period rate. Months
    with fewer than `min_segment_days` of coverage are skipped.
    """
    out: List[MonthlyBEI] = []

    # Anchor t=0 at today; tenors in years.
    current_month_start = date(today.year, today.month, 1)
    cursor = current_month_start
    for i in range(months_ahead):
        m_start = cursor if i > 0 else today
        m_end_inclusive = date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1])
        days_in_month = (m_end_inclusive - cursor).days + 1
        days_in_segment = (m_end_inclusive - m_start).days + 1
        if days_in_segment < min_segment_days:
            cursor = _next_month_start(cursor)
            continue

        t1 = (m_start - today).days / 365.25
        t2 = (m_end_inclusive - today).days / 365.25 + 1.0 / 365.25  # next-day start
        if t1 < 0:
            t1 = 0.0
        if t2 <= t1:
            cursor = _next_month_start(cursor)
            continue

        if t1 == 0:
            # First (partial) month: just use spot rates to t2.
            fwd_nom = nom_curve(t2)
            fwd_real = real_curve(t2)
        else:
            fwd_nom = forward_rate(nom_curve, t1, t2)
            fwd_real = forward_rate(real_curve, t1, t2)

        if fwd_nom is None or fwd_real is None:
            cursor = _next_month_start(cursor)
            continue

        # Forward BEI annualized over the segment, then de-annualized to monthly.
        bei_annual = fisher_break_even(fwd_nom, fwd_real)
        # Convert TEA over the segment to the equivalent rate for a 30-day month:
        # (1 + bei_annual)^(days_in_month/365) − 1, then scale by partial coverage
        # if we only saw part of the month.
        try:
            scale = days_in_month / max(days_in_segment, 1)
            monthly_bei = (1.0 + bei_annual) ** (days_in_month / 365.0) - 1.0
            # When the segment is partial, the BEI we computed already represents
            # the rate for that shorter period; rescale to a full month equivalent.
            if scale != 1.0:
                monthly_bei = (1.0 + bei_annual) ** (days_in_segment / 365.0) - 1.0
                monthly_bei = (1.0 + monthly_bei) ** scale - 1.0
        except (ValueError, OverflowError):
            monthly_bei = None

        out.append(MonthlyBEI(
            label=f"{_MONTH_ES[cursor.month - 1]}-{cursor.year % 100:02d}",
            month_start=cursor,
            month_end=m_end_inclusive,
            days_in_month=days_in_month,
            monthly_bei=monthly_bei,
            annualized_bei=bei_annual,
            forward_nominal=fwd_nom,
            forward_real=fwd_real,
        ))
        cursor = _next_month_start(cursor)
    return out
