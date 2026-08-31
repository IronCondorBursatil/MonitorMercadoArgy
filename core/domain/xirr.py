"""Solver de TIR (XIRR) — Brent con auto-bracketing robusto + la constante de año
Act/365.25.

Diseño (post-migración a Brent):
  - `_npv` es overflow-safe: ante tasas absurdas devuelve un valor no-finito en vez
    de propagar OverflowError.
  - `_xirr_from_years` intenta primero un Newton rápido (5 seeds); SÓLO acepta su
    resultado si converge limpio (finito, > -1, |NPV| < tol). Si no, cae a
    `_bracket_and_solve` (brentq con bracket auto-expandible) — robustez de bisección.
  - `xirr(flows, dates, day_count=None)` descuenta con la convención pedida (vía
    `daycount.year_fraction`); sin `day_count` usa años julianos 365.25 (back-compat
    de los callers viejos y del default soberano).

El bracket viejo era fijo `[-0.999, 10.0]` → perdía yields > 1000% y tasas en
`(-1, -0.999)`. El auto-bracket nuevo expande el extremo superior geométricamente
hasta encontrar cambio de signo, cubriendo cualquier yield real (y devuelve NaN
limpio cuando la NPV es monótona y no tiene raíz).
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

import numpy as np
from scipy.optimize import brentq, newton

# Year length for act/365.25 TIR convention (Julian calendar average).
# Se mantiene acá porque lo importan tamar.py, yield_curve.py, inflation_path.py.
_JULIAN_YEAR = 365.25

# XIRR Newton seeds — span typical sovereign/corporate yields in AR.
_XIRR_GUESSES = (0.05, 0.20, -0.10, 0.80, -0.50)
_XIRR_TOLERANCE = 1e-4

_BRACKET_LO = -0.9999          # justo por encima del polo en r = -1
_BRACKET_HI0 = 1.0             # extremo superior inicial (100%)
_BRACKET_MAX_GROW = 60         # 1.0 · 2^60 ≈ 1.15e18 → cubre cualquier yield real


def _npv(flows: np.ndarray, years: np.ndarray, rate: float) -> float:
    """NPV a la tasa `rate`. Overflow-safe: devuelve no-finito (±inf) en vez de
    tirar OverflowError ante tasas absurdas. En el límite r→-1+ la NPV tiende a
    +∞ (hay egreso temprano y flujos futuros descontados explotan)."""
    if rate <= -1.0:
        return 1e18
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        return float(np.sum(flows / (1.0 + rate) ** years))


def _bracket_and_solve(npv) -> float:
    """brentq con auto-bracketing: arranca en [LO, 1.0] y expande el extremo
    superior (×2) hasta que la NPV cambia de signo; entonces brentq. Sin cambio
    de signo (NPV monótona: all-positive / all-negative / un solo flujo) → NaN."""
    f_lo = npv(_BRACKET_LO)
    if not np.isfinite(f_lo):
        return np.nan

    hi = _BRACKET_HI0
    f_hi = npv(hi)
    bracketed = False
    for _ in range(_BRACKET_MAX_GROW):
        # Cambio de signo ⇒ hay raíz en [lo, hi]. `np.sign(0)=0`, así que un
        # endpoint exactamente nulo cuenta como cambio (brentq lo resuelve), pero
        # NPV≡0 (todo flujo cero) NO bracketea (sign 0 vs 0) → NaN, lo correcto.
        if np.isfinite(f_hi) and np.sign(f_hi) != np.sign(f_lo):
            bracketed = True
            break
        hi *= 2.0
        f_hi = npv(hi)
    if not bracketed:
        return np.nan

    try:
        return float(brentq(npv, _BRACKET_LO, hi, maxiter=200, xtol=1e-12))
    except (RuntimeError, ValueError):
        return np.nan


def _xirr_from_years(flows: np.ndarray, years: np.ndarray,
                     day_count: Optional[object] = None) -> float:
    """XIRR con fracciones de año pre-calculadas (cualquier day-count).

    `day_count` se acepta por compatibilidad de firma pero se ignora: los `years`
    ya vienen descontados con la convención correcta por el caller.
    """
    flows = np.asarray(flows, dtype=float)
    years = np.asarray(years, dtype=float)
    if flows.size < 2 or not np.any(flows):
        return np.nan

    def npv(rate):
        return _npv(flows, years, rate)

    # Pre-paso Newton (rápido). Se acepta sólo si converge limpio.
    for guess in _XIRR_GUESSES:
        try:
            r = newton(npv, guess, maxiter=50)
        except (RuntimeError, ValueError, OverflowError, FloatingPointError):
            continue
        if np.isfinite(r) and r > -1.0 and abs(npv(r)) < _XIRR_TOLERANCE:
            return float(r)

    return _bracket_and_solve(npv)


def xirr(flows: List[float], dates: List[date],
         day_count: Optional[object] = None) -> float:
    """TIR de un stream de (flujo, fecha). Con `day_count` descuenta con esa
    convención; sin él, años julianos 365.25 (default soberano / back-compat)."""
    if not flows or len(flows) < 2:
        return np.nan
    d0 = dates[0]
    if day_count is None:
        years = np.array([(d - d0).days / _JULIAN_YEAR for d in dates])
    else:
        from core.domain.daycount import year_fraction
        years = np.array([year_fraction(d0, d, day_count) for d in dates])
    return _xirr_from_years(np.array(flows, dtype=float), years)
