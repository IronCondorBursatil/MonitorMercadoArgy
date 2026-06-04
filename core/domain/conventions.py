"""Convenciones de mercado puras: day-count, conversiones de tasa, fórmula
TAMAR_TEM, settlement BYMA y fecha de referencia CER.

Extraído de `services.py` SIN cambios de fórmula. `days_30_360` se re-exporta
desde `cashflow_synth` (única fuente de verdad del 30/360).
"""

from __future__ import annotations

import threading
from datetime import date, timedelta
from typing import Optional

# Re-export: única fuente de verdad del day-count 30/360.
from core.domain.cashflow_synth import days_30_360  # noqa: F401
from core.holiday_engine import (  # noqa: F401  (re-export)
    es_habil, is_habil, settlement_byma, settlement_byma_date,
)

# --------------------------------------------------------------------------- #
# Fórmula oficial BONTE TAMAR: TAMAR_TEM = ((1 + TNA/k)^k)^(1/12) − 1, k=365/32.
# --------------------------------------------------------------------------- #
_TAMAR_K = 365.0 / 32.0  # ≈ 11.40625 (períodos de 32 días por año)


def tamar_tem(tna_dec: Optional[float]) -> Optional[float]:
    """TAMAR (TNA decimal) → TEM (decimal) via fórmula oficial BONTE TAMAR."""
    if tna_dec is None:
        return None
    try:
        return ((1.0 + tna_dec / _TAMAR_K) ** _TAMAR_K) ** (1.0 / 12.0) - 1.0
    except (ValueError, OverflowError, ZeroDivisionError):
        return None


# --------------------------------------------------------------------------- #
# Conversiones TEA ↔ TEM ↔ TNA (idénticas a las staticmethods originales).
# --------------------------------------------------------------------------- #
def tea_to_tem(tea: Optional[float]) -> Optional[float]:
    """TEA → TEM, act/365: (1+TEA)^(30/365) − 1."""
    if tea is None or tea <= -1.0:
        return None
    try:
        return (1.0 + tea) ** (30.0 / 365.0) - 1.0
    except (ValueError, OverflowError):
        return None


def tea_to_tna_monthly(tea: Optional[float]) -> Optional[float]:
    """TEA → TNA m=12 (mensual capitalizable): 12 × ((1+TEA)^(1/12) − 1)."""
    if tea is None or tea <= -1.0:
        return None
    try:
        return 12.0 * ((1.0 + tea) ** (1.0 / 12.0) - 1.0)
    except (ValueError, OverflowError):
        return None


def tea_to_tna(tea: Optional[float]) -> Optional[float]:
    """TEA → TNA, base 365: 365 × ((1+TEA)^(1/365) − 1)."""
    if tea is None or tea <= -1.0:
        return None
    try:
        return 365.0 * ((1.0 + tea) ** (1.0 / 365.0) - 1.0)
    except (ValueError, OverflowError):
        return None


def tea_to_tna_freq(tea: Optional[float], freq: int) -> Optional[float]:
    """TEA → TNA nominal a la frecuencia de pago m=freq: freq × ((1+TEA)^(1/freq) − 1).

    Es la convención de "TIR Nominal" del informe IAMC/Balanz para bonos CON cupón:
    la tasa nominal anual capitalizable a la frecuencia del cupón (semestral → freq=2,
    trimestral → freq=4). Distinta de `tea_to_tna` (base 365, diaria) y de
    `tea_to_tna_monthly` (m=12) — esas se reservan para peso/TAMAR/LECAP."""
    if tea is None or tea <= -1.0 or not freq or freq <= 0:
        return None
    try:
        return freq * ((1.0 + tea) ** (1.0 / freq) - 1.0)
    except (ValueError, OverflowError, ZeroDivisionError):
        return None


def tea_to_tem_m12(tea: Optional[float]) -> Optional[float]:
    """TEA → TEM mensual m=12 (30/360 Secretaría de Finanzas): (1+TEA)^(1/12) − 1."""
    if tea is None or tea <= -1.0:
        return None
    try:
        return (1.0 + tea) ** (1.0 / 12.0) - 1.0
    except (ValueError, OverflowError):
        return None


# --------------------------------------------------------------------------- #
# Settlement BYMA (cache diaria) + fecha de referencia CER.
# El cómputo es date-native (~1µs, lookup O(1) en frozenset de feriados). La cache
# diaria (T+0/T+1, invalidación por día calendario) queda como conveniencia barata.
# --------------------------------------------------------------------------- #
_SETTLE_CACHE: dict = {}     # {lag: settle_date}
_SETTLE_CACHE_DAY: Optional[date] = None
_SETTLE_CACHE_LOCK = threading.Lock()


def settlement_for(instrument_type: str) -> date:
    """Liquidación por defecto (T+1) para el refresh loop normal. `instrument_type`
    se mantiene en la firma por compat; la convención actual es lag=1 para todos."""
    global _SETTLE_CACHE_DAY
    lag = 1
    today = date.today()
    with _SETTLE_CACHE_LOCK:
        if _SETTLE_CACHE_DAY != today:
            _SETTLE_CACHE.clear()
            _SETTLE_CACHE_DAY = today
        cached = _SETTLE_CACHE.get(lag)
        if cached is not None:
            return cached
    # date-native (sin round-trip de string) — fuera del lock.
    settle = settlement_byma_date(today, lag=lag)
    with _SETTLE_CACHE_LOCK:
        _SETTLE_CACHE[lag] = settle
    return settle


def resolve_settle(instrument_type: str, override: Optional[date]) -> date:
    """Usa el override si se provee (toggle T+0/T+1 del popup), si no la
    convención por tipo."""
    return override if override is not None else settlement_for(instrument_type)


def cer_reference_date(settle: date, lag_business_days: int) -> date:
    """Camina hacia atrás `lag_business_days` días hábiles desde `settle`.
    Usa `es_habil` date-native (O(1) por día) — antes el `is_habil(strftime)`
    hacía un round-trip date→str→Timestamp por cada uno de los ~10 días."""
    target = settle
    count = 0
    while count < lag_business_days:
        target -= timedelta(days=1)
        if es_habil(target):
            count += 1
    return target
