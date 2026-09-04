"""Day-counts — fuente ÚNICA de verdad de la fracción de año para DESCONTAR.

Antes el día-count de descuento estaba cableado: todo lo que no era 30/360 caía a
`_JULIAN_YEAR = 365.25` (en `xirr.py`, `pricing/base.py`, `pricing/metrics.py`,
`pricing/strategies.py`), IGNORANDO el campo `day_count` del instrumento. Las ONs
hard-dollar declaran "ACT/365" pero se descontaban a 365.25 → TIR con error de ~1bp.

Este módulo centraliza el cálculo en una única `year_fraction(start, end, conv)`.
Cada strategy/metric obtiene la convención del instrumento (`Instrument.day_count_enum`)
y descuenta con ella. Garantía de equivalencia: para `THIRTY_360` y `ACT_365_25` el
resultado es bit-idéntico a las fórmulas viejas → esos bonos NO cambian; sólo se
corrige el descuento de los ACT/365 (ONs + Dólar-Linked).

`days_30_360` sigue viviendo en `cashflow_synth.py` (lo importa `_legacy_engine.py` y
la síntesis); acá se re-exporta para que el código de pricing importe todo desde un
solo lugar.
"""

from __future__ import annotations

import calendar
from datetime import date
from enum import Enum
from functools import lru_cache
from typing import Optional

from core.domain.cashflow_synth import days_30_360  # noqa: F401  re-export (fuente única 30/360)

# Año juliano (promedio del calendario, incluye bisiestos). Convención de los
# soberanos AR y default histórico del motor. Se mantiene también en `xirr.py`.
_JULIAN_YEAR = 365.25


class DayCount(str, Enum):
    """Convenciones de base de cálculo soportadas. `str`-enum → barato de
    comparar/serializar y el `.value` es el string canónico del Excel/ABM."""
    ACT_365 = "ACT/365"
    ACT_365_25 = "ACT/365.25"
    THIRTY_360 = "30/360"
    ACT_ACT = "ACT/ACT"


_DEFAULT = DayCount.ACT_365_25


def _parse_day_count_impl(raw: Optional[str]) -> DayCount:
    """Implementación (sin memo) de `parse_day_count`. Ver su docstring."""
    if raw is None:
        return _DEFAULT
    s = str(raw).strip().upper().replace(" ", "")
    if not s:
        return _DEFAULT
    s = s.replace("ACTUAL", "ACT").replace(",", ".")
    if "30" in s and "360" in s:
        return DayCount.THIRTY_360
    if "ACT/ACT" in s or s.count("ACT") >= 2:
        return DayCount.ACT_ACT
    if "365.25" in s:
        return DayCount.ACT_365_25
    if "365" in s:
        return DayCount.ACT_365
    return _DEFAULT


# Memo: `Instrument.day_count_enum` llama acá POR CASHFLOW en cada `year_fraction_to`
# (~13.300 parseos por ciclo de pricing) sobre un dominio REAL de 4-5 grafías. La
# función es PURA (string → enum: sin estado, sin I/O, sin reloj) y el `DayCount` que
# devuelve es un enum inmutable ⇒ memoizarla no puede mover un número.
# `maxsize` acotado a propósito: la grafía viene de datos (Excel/ABM), así que un memo
# sin techo sería un crecimiento dirigible por input.
_parse_day_count_cached = lru_cache(maxsize=128)(_parse_day_count_impl)


def parse_day_count(raw: Optional[str] = None) -> DayCount:
    """Parsea el string de `base calculo` a un `DayCount`, tolerante a alias,
    mayúsculas, espacios, coma decimal y basura. Desconocido/None → ACT/365.25
    (el default soberano). NUNCA lanza.

    Orden importante: chequear "365.25" ANTES de "365" (prefijo) y "ACT/ACT"
    antes de "ACT/365".

    Memoizada (ver `_parse_day_count_cached`). El wrapper existe para preservar el
    contrato "NUNCA lanza": `lru_cache` pelado tira `TypeError: unhashable type` ante
    un argumento no hasheable (una lista/dict que se cuele desde el ABM o un parser),
    donde la implementación devolvía el default. Ese camino degrada al parseo directo.
    """
    try:
        return _parse_day_count_cached(raw)
    except TypeError:            # argumento no hasheable → sin memo, mismo resultado
        return _parse_day_count_impl(raw)


parse_day_count.cache_clear = _parse_day_count_cached.cache_clear
parse_day_count.cache_info = _parse_day_count_cached.cache_info


def _act_act_isda(start: date, end: date) -> float:
    """ISDA actual/actual: suma, por cada año calendario tocado, los días que caen
    en ese año divididos por su largo (366 bisiesto, 365 si no). Un año completo
    da 1.0 exacto sea bisiesto o no."""
    if start == end:
        return 0.0
    if end < start:
        return -_act_act_isda(end, start)
    total = 0.0
    y = start.year
    while y <= end.year:
        year_start = max(start, date(y, 1, 1))
        year_end = min(end, date(y + 1, 1, 1))
        if year_end > year_start:
            denom = 366.0 if calendar.isleap(y) else 365.0
            total += (year_end - year_start).days / denom
        y += 1
    return total


def year_fraction(start: date, end: date, convention: DayCount) -> float:
    """LA fracción de año de descuento ref→target bajo `convention`.

    - THIRTY_360 → days_30_360(start,end)/360  (idéntico a la rama is_30_360 vieja)
    - ACT_365    → días reales / 365
    - ACT_365_25 → días reales / 365.25         (idéntico al camino vanilla viejo)
    - ACT_ACT    → ISDA actual/actual

    Same-day → 0.0. Preserva el signo si end < start (negativo para ACT; 30/360 ya
    viene firmado desde days_30_360).
    """
    if end == start:
        return 0.0
    if convention is DayCount.THIRTY_360:
        return days_30_360(start, end) / 360.0
    if convention is DayCount.ACT_ACT:
        return _act_act_isda(start, end)
    if convention is DayCount.ACT_365:
        return (end - start).days / 365.0
    return (end - start).days / _JULIAN_YEAR  # ACT_365_25 (default)
