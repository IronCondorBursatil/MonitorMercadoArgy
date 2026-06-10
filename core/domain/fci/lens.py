"""Lente de moneda del panel FCI: convierte retornos a USD (vía MEP) y reales (vía CER).

Computa, por período (7d/1m/3m/6m/ytd/12m), la **devaluación** (proxy A3500) y la **inflación**
(CER) reales a partir de las series de BCRA, y el MEP spot actual para expresar la cuotaparte en
dólares. Funciones puras (toman dicts {date: valor}) → testeables sin red.

Si un período cae fuera de la cobertura de la serie (historia corta), devuelve `None`; el front
degrada a nominal (`macro || 0`). La cobertura crece sola al bootstrappear ~400d en
`indices_provider.py`.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Optional

from core.domain.fci import PERIOD_DAYS


def _asof(series: Dict[date, float], d: date) -> Optional[float]:
    """Último valor con fecha <= d (None si d precede a toda la serie)."""
    ks = [k for k in series if k <= d]
    return series[max(ks)] if ks else None


def _pct_change(series: Dict[date, float], base: date, days: int) -> Optional[float]:
    """% de cambio entre (base - days) y base. None si la serie no cubre el inicio."""
    if not series:
        return None
    target = base - timedelta(days=days)
    if target < min(series):          # fuera de cobertura → sin dato (no inventar 0)
        return None
    start, end = _asof(series, target), _asof(series, base)
    if start and end and start > 0:
        return round((end / start - 1.0) * 100.0, 4)
    return None


def compute_macro(cer_series: Dict[date, float], a3500_series: Dict[date, float],
                  mep_now: Optional[float], fecha_base: Optional[str]) -> dict:
    """`{mep{period:%}, cer{period:%}, mep_now}` con datos reales. mep usa A3500 (proxy FX)."""
    base = date.fromisoformat(fecha_base) if fecha_base else date.today()
    ytd_days = (base - date(base.year, 1, 1)).days or 1

    def block(series):
        out = {p: _pct_change(series, base, d) for p, d in PERIOD_DAYS.items()}
        out["ytd"] = _pct_change(series, base, ytd_days)
        return out

    return {"mep": block(a3500_series), "cer": block(cer_series),
            "mep_now": mep_now if (mep_now and mep_now > 0) else None}
