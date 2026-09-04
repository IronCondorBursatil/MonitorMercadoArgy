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

import math
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

# VENTANA del atajo cerrado = exactamente el rango que `_bracket_and_solve` puede
# bracketear: `[_BRACKET_LO, 2^(MAX_GROW−1)]` (evalúa el extremo superior en 2^0…2^59).
# Una raíz FUERA de esa ventana el camino histórico NO la encontraba: devolvía NaN
# → TIR None → celda vacía en el panel. El atajo se frena en los mismos bordes a
# propósito: su trabajo es ahorrar iteraciones, no empezar a publicar TIRs que antes
# no existían. Ambos bordes son casos REALES del catálogo, detectados por el oráculo
# de `tests/test_perf_W1_dominio_xirr.py` (S12J6, a un día del vencimiento: a la mitad
# del payoff da r ≈ 8.9e109 y a 1,5× da r ≈ −0.99999998; el solver viejo daba NaN en
# los dos). Fuera de la ventana se cae al camino de siempre, que decide como siempre.
_CLOSED_FORM_MAX_RATE = _BRACKET_HI0 * 2.0 ** (_BRACKET_MAX_GROW - 1)
_CLOSED_FORM_MIN_RATE = _BRACKET_LO


def _npv(flows: np.ndarray, years: np.ndarray, rate: float) -> float:
    """NPV a la tasa `rate`. Overflow-safe: devuelve no-finito (±inf) en vez de
    tirar OverflowError ante tasas absurdas. En el límite r→-1+ la NPV tiende a
    +∞ (hay egreso temprano y flujos futuros descontados explotan).

    El `np.errstate` NO va acá: se iza al caller (`_xirr_from_years`), que llama a
    esta función ~20.900 veces por ciclo de pricing. Entrar y salir del context
    manager en cada llamada costaba más que la cuenta misma."""
    if rate <= -1.0:
        return 1e18
    return float((flows / (1.0 + rate) ** years).sum())


def _closed_form_two_flows(flows: np.ndarray, years: np.ndarray) -> float:
    """IRR **CERRADA** del stream de DOS flujos: `r = (pago/precio)^(1/Δt) − 1`.

    Un stream `(−precio en t0, pago en t1)` tiene UNA sola raíz y es algebraica:
        NPV(r) = f0·(1+r)^−t0 + f1·(1+r)^−t1 = 0
              ⇒ (1+r)^(t1−t0) = −f1/f0
              ⇒ r = (−f1/f0)^(1/(t1−t0)) − 1
    Son ~100 patas del catálogo (LECAP / BONCAP / LECER / BONCER ZC y cualquier bono
    al que le quede un solo flujo) que hoy pagan el root-finder entero — hasta 5
    arranques de secante, cada uno con sus ~20 evaluaciones de NPV — para una cuenta
    de una línea. Vive acá, en el SOLVER, y no en cada strategy: así lo aprovechan
    todas (vanilla, CER, dólar-linked) sin duplicar la condición por tipo.

    NaN fuera de su dominio (el caller sigue por el camino de siempre):
      - `flows.size != 2`;
      - sin **un único cambio de signo** (`f0 < 0 < f1`) — sin eso la raíz no es única
        ni positiva y la fórmula no aplica;
      - `Δt ≤ 0` o no finito (mismo instante, o flujo "hacia atrás");
      - overflow: `ratio**(1/Δt)` puede desbordar con Δt diminuto. Se opera con floats
        de Python **a propósito** (no `np.float64`): Python LANZA `OverflowError` —
        que se atrapa acá — donde numpy devolvería `inf` en silencio.
      - raíz fuera de `[_CLOSED_FORM_MIN_RATE, _CLOSED_FORM_MAX_RATE]`, la ventana que
        el bracketing histórico alcanzaba (ver esas constantes). El atajo NO puede
        devolver TIRs que el camino viejo no encontraba.

    El resultado siempre cumple `r > −1` (una potencia de un ratio positivo es
    positiva), así que no puede colarse la tasa degenerada que el caller descarta.

    CUÁNTO MUEVE LA TIR (medido, no supuesto): el atajo devuelve la raíz EXACTA y el
    root-finder cortaba en `|NPV| < 1e-4`, así que los dos difieren en el residuo de la
    secante. Oráculo ON/OFF del motor completo sobre el catálogo VIVO (1.158
    instrumentos × 3 precios × 19 métricas): **9,4e-13 relativo** en el peor caso
    (S17L6, `duration`), 109 tickers tocados, sólo métricas derivadas de la TIR, y
    **ninguna** TIR que aparezca o desaparezca (sin flips None↔número). El límite lo
    fija de forma permanente `tests/test_perf_W1_dominio_xirr.py::
    test_oraculo_catalogo_real_mono_flujo` en 1e-9 relativo.
    """
    if flows.size != 2:
        return np.nan
    f0 = float(flows[0])
    f1 = float(flows[1])
    if not (f0 < 0.0 < f1):       # NaN cae acá: toda comparación con NaN es False
        return np.nan
    dt = float(years[1]) - float(years[0])
    if not (dt > 0.0) or not math.isfinite(dt):
        return np.nan
    try:
        r = (-f1 / f0) ** (1.0 / dt) - 1.0
    except (OverflowError, ZeroDivisionError, ValueError):
        return np.nan
    if not math.isfinite(r) or not (_CLOSED_FORM_MIN_RATE <= r <= _CLOSED_FORM_MAX_RATE):
        return np.nan
    return r


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
                     day_count: Optional[object] = None,
                     seed: Optional[float] = None) -> float:
    """XIRR con fracciones de año pre-calculadas (cualquier day-count).

    `day_count` se acepta por compatibilidad de firma pero se ignora: los `years`
    ya vienen descontados con la convención correcta por el caller.

    `seed` (opcional, warm-start): tasa a probar como PRIMER arranque de Newton —
    típicamente la TIR del ciclo anterior, que con precios que casi no se mueven
    converge en 2-3 iteraciones en vez de las ~20 del seed frío 0.05. Se somete al
    MISMO criterio de aceptación que los demás; si no lo pasa, se sigue con
    `_XIRR_GUESSES` y con brentq exactamente como siempre. **Con `seed=None` el
    camino recorrido es idéntico al histórico** (misma tupla de guesses, sin
    construir nada).

    ⚠️ El solver acepta el PRIMER arranque que converge a `|NPV| < 1e-4`, así que un
    seed distinto PUEDE devolver un `r` distinto en los últimos dígitos. Antes de
    cablearlo hay que medirlo sobre el catálogo real:
    `tests/test_perf_W1_dominio_xirr_seed.py`.
    """
    flows = np.asarray(flows, dtype=float)
    years = np.asarray(years, dtype=float)
    if flows.size < 2 or not np.any(flows):
        return np.nan

    def npv(rate):
        return _npv(flows, years, rate)

    # `errstate` IZADO: cubre las ~20.900 evaluaciones de npv por ciclo con UN solo
    # context manager en vez de uno por llamada. Tiene que envolver también a
    # `_bracket_and_solve`, que evalúa npv con `hi` hasta ~1,15e18 (overflow esperado
    # y tratado: el bracketing descarta los no-finitos).
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        # Atajo algebraico: dos flujos con un solo cambio de signo ⇒ raíz única y
        # cerrada, sin iterar. NaN = fuera de su dominio ⇒ camino de siempre.
        r = _closed_form_two_flows(flows, years)
        if math.isfinite(r):
            return r

        guesses = _XIRR_GUESSES
        if seed is not None:
            try:
                s = float(seed)
            except (TypeError, ValueError):
                s = None
            if s is not None and math.isfinite(s) and s > -1.0:
                guesses = (s,) + _XIRR_GUESSES

        # Pre-paso Newton (rápido). Se acepta sólo si converge limpio.
        for guess in guesses:
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
