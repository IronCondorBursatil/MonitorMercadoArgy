"""Cálculo de vencimiento BYMA: tercer viernes del mes codificado en el ticker.

Si hay feriado en el tercer viernes, BYMA típicamente mueve al hábil previo —
si el `holiday_engine` tiene un calendario disponible se respeta; caso contrario
se cae al tercer viernes calendario (suficiente para mocks/visualización; el
margen de 1 día no impacta el pricing significativamente).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional


def third_friday(year: int, month: int) -> date:
    """Tercer viernes calendario del mes (sin chequear feriados)."""
    d = date(year, month, 1)
    # weekday(): Monday=0, Friday=4
    first_friday = 1 + ((4 - d.weekday()) % 7)
    return date(year, month, first_friday + 14)


def resolve_expiry_date(month: int, today: Optional[date] = None,
                        holiday_engine=None) -> date:
    """Devuelve el tercer viernes (con fallback a hábil previo si hay engine).

    `month` es 1-12 sin año; el año se infiere: si el tercer viernes de este año
    ya pasó (o pasa en los próximos 5 días), se asume el del año siguiente.
    Esto matchea el comportamiento real del listado BYMA (los meses cortos
    aparecen como vto del año en curso; los lejanos como año siguiente).
    """
    today = today or date.today()
    candidate = third_friday(today.year, month)
    if candidate <= today + timedelta(days=5):
        candidate = third_friday(today.year + 1, month)
    if holiday_engine is not None:
        try:
            # Si el engine tiene .is_business_day o .next_business_day, lo usamos.
            while not holiday_engine.is_business_day(candidate):  # type: ignore[attr-defined]
                candidate -= timedelta(days=1)
        except AttributeError:  # engine sin is_business_day → dejar el 3er viernes
            pass
    return candidate


def days_to_expiry(expiry: date, today: Optional[date] = None) -> int:
    """Días calendario hasta el vencimiento (≥ 1)."""
    today = today or date.today()
    return max(1, (expiry - today).days)


def time_to_expiry(expiry: date, today: Optional[date] = None) -> float:
    """T en años (act/365)."""
    return days_to_expiry(expiry, today) / 365.0
