"""Histórico de cuotaparte (VCP) para el chart 'Cuotaparte' del detalle.

CAFCI solo publica el último corte de VCP. Dos fuentes:
  - `reconstruct_hist`: serie base-100 (HIST_N pts, ~1 año) ANCLADA a los retornos reales
    por período (interp log-lineal entre anclas + ruido determinístico ínfimo). No inventa
    retornos: pasa por los retornos reales 7d/1m/3m/6m/12m.
  - `merge_real_hist`: cuando `fci_history` ya acumuló suficiente serie real de VCP, la usa
    (base-100, forward-fill sobre el eje); si no cubre la ventana, cae a la reconstruida.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Dict, List, Optional, Tuple

from core.domain.fci import ANCHORS, HIST_N
from core.domain.fci.derive import seed


def reconstruct_hist(vcp, rend, key, n: int = HIST_N) -> Optional[List[float]]:
    """Serie base-100 (oldest→hoy) que pasa por las anclas de retorno real. None si no hay vcp
    o menos de 2 anclas."""
    if not vcp:
        return None
    pts = [(0, 1.0)]
    for per, days in ANCHORS:
        d = (rend.get(per) or {}).get("directo")
        if d is None:
            continue
        pts.append((days, 1.0 / (1.0 + d / 100.0)))
    pts = sorted(set(pts))
    if len(pts) < 2:
        return None
    max_d = pts[-1][0]
    xs = [p[0] for p in pts]
    ys = [math.log(p[1]) for p in pts]
    series = []
    for i in range(n):
        days_ago = max_d * (1 - i / (n - 1))   # oldest → hoy
        ly = ys[-1]
        for j in range(len(xs) - 1):
            if xs[j] <= days_ago <= xs[j + 1] or j == len(xs) - 2:
                t = (days_ago - xs[j]) / (xs[j + 1] - xs[j]) if xs[j + 1] != xs[j] else 0
                ly = ys[j] + t * (ys[j + 1] - ys[j])
                break
        noise = (seed(key, "h", i) - 0.5) * 0.004
        series.append(math.exp(ly + noise))
    base = series[0]
    return [round(v / base * 100, 3) for v in series]


def merge_real_hist(reconstructed: Optional[List[float]],
                    real_series: Dict[date, dict],
                    axis: List[str]) -> Tuple[Optional[List[float]], bool]:
    """Si la serie REAL de VCP (de fci_history) cubre la ventana del eje, devuelve una
    base-100 desde VCP real (forward-fill) + True. Si no, la reconstruida + False."""
    real_dates = sorted(d for d, v in (real_series or {}).items() if (v or {}).get("vcp"))
    if len(real_dates) < 2 or not axis:
        return reconstructed, False
    # cobertura suficiente: el primer dato real cae en el primer cuarto del eje
    cover_cut = date.fromisoformat(axis[len(axis) // 4])
    if real_dates[0] > cover_cut:
        return reconstructed, False
    vals: List[float] = []
    for ds in axis:
        d = date.fromisoformat(ds)
        v = None
        for rd in real_dates:
            if rd <= d:
                v = real_series[rd]["vcp"]
            else:
                break
        if v is None:
            v = real_series[real_dates[0]]["vcp"]
        vals.append(v)
    base = vals[0] or 1.0
    return [round(v / base * 100, 3) for v in vals], True
