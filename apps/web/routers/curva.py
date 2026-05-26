"""Curva TIR vs MD (Chart.js scatter + ajuste log).

GET /curva            → página con selector de grupo + canvas.
GET /curva/data?grupo → JSON {points:[{ticker,x,y}], fit:[{x,y}], label}.

Reusa AppState (metrics) + el fit log de panels (`_fit_log_curve`). Curvas
homogéneas: Soberanos USD (sufijo D), CER, Tasa Fija, TAMAR.
"""

from __future__ import annotations

import math
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from apps.web.deps import get_state
from apps.web.routers.panels import _fit_log_curve
from core.domain.portfolio import position_currency

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# clave -> (label, {types}, currency-filter|None)
_CURVA_GROUPS = {
    "soberanos_usd": ("Soberanos USD", {"BONAR", "GLOBAL", "BOPREAL"}, "USD"),
    "cer": ("CER (real)", {"CER", "LECER", "BONCER", "BONCER ZC", "CON CUPON", "STEP-UP"}, None),
    "tasa_fija": ("Tasa Fija (nominal)", {"LECAP", "BONCAP", "BONOFIJA"}, None),
    "tamar": ("TAMAR / Dual", {"PURO", "DUAL", "DUAL_CER_TAMAR"}, None),
}


@router.get("/curva", response_class=HTMLResponse)
def curva_page(request: Request):
    groups = [{"key": k, "label": v[0]} for k, v in _CURVA_GROUPS.items()]
    return _TEMPLATES.TemplateResponse(request, "pages/curva.html", {"groups": groups})


@router.get("/curva/data")
def curva_data(grupo: str = "soberanos_usd", state=Depends(get_state)):
    if grupo not in _CURVA_GROUPS:
        grupo = "soberanos_usd"
    label, types, ccy = _CURVA_GROUPS[grupo]
    metrics = []
    for m in state.metrics():
        inst = m.snapshot.instrument if m.snapshot else None
        if not inst or inst.instrument_type not in types:
            continue
        if m.duration is None or m.duration <= 0 or m.tir is None:
            continue
        if ccy and position_currency(inst.instrument_type, inst.ticker) != ccy:
            continue
        metrics.append(m)

    points = sorted(
        ({"ticker": m.snapshot.instrument.ticker, "x": round(m.duration, 3), "y": round(m.tir * 100, 3)}
         for m in metrics),
        key=lambda p: p["x"],
    )
    fit_pts = []
    fit = _fit_log_curve(metrics)
    if fit and points:
        a, b = fit
        x0, x1 = points[0]["x"], points[-1]["x"]
        n = 24
        for i in range(n + 1):
            x = x0 + (x1 - x0) * i / n
            if x > 0:
                fit_pts.append({"x": round(x, 3), "y": round((a + b * math.log(x)) * 100, 3)})
    return JSONResponse({"points": points, "fit": fit_pts, "label": label})
