"""XIRR solver (Newton + Brentq fallback) y la constante de año Act/365.25.

Extraído de `services.py` SIN cambios de fórmula — es la fuente de verdad del
descuento Act/365.25 usada por las pricing strategies y la fachada.
"""

from __future__ import annotations

from datetime import date
from typing import List

import numpy as np
from scipy.optimize import brentq, newton

# Year length for act/365.25 TIR convention (Julian calendar average).
_JULIAN_YEAR = 365.25

# XIRR Newton seeds — span typical sovereign/corporate yields in AR.
_XIRR_GUESSES = (0.05, 0.20, -0.10, 0.80, -0.50)
_XIRR_TOLERANCE = 1e-4


def _xirr_from_years(flows: np.ndarray, years: np.ndarray) -> float:
    """XIRR con fracciones de año pre-calculadas (cualquier day-count)."""
    def npv(rate):
        if rate <= -1.0:
            return 1e12
        return np.sum(flows / (1 + rate) ** years)
    for guess in _XIRR_GUESSES:
        try:
            res = newton(npv, guess, maxiter=50)
            if not np.isnan(res) and abs(npv(res)) < _XIRR_TOLERANCE:
                return res
        except (RuntimeError, ValueError, OverflowError):
            continue
    try:
        return brentq(npv, -0.999, 10.0)
    except (RuntimeError, ValueError):
        return np.nan


def xirr(flows: List[float], dates: List[date]) -> float:
    if not flows or len(flows) < 2:
        return np.nan
    d0 = dates[0]
    years = np.array([(d - d0).days / _JULIAN_YEAR for d in dates])
    return _xirr_from_years(np.array(flows), years)
