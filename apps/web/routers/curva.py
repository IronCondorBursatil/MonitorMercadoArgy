"""Curva TIR vs MD (Chart.js scatter + ajuste log).

GET /curva            → página con selector de grupo + canvas.
GET /curva/data?grupo → JSON {points:[{ticker,x,y}], fit:[{x,y}], label}.

Reusa AppState (metrics) + el fit log de panels (`_fit_log_curve`). Curvas
homogéneas: Soberanos USD (sufijo D), CER, Tasa Fija, TAMAR.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from apps.web.deps import get_state
from apps.web.panels_rows import _fit_log_curve
from apps.web.templates import TEMPLATES as _TEMPLATES
from core.domain.portfolio import position_fx_leg

router = APIRouter()

# clave -> (label, {types}, fx-leg-filter|None)
#
# El tercer campo filtra por **pata de FX** (`portfolio.position_fx_leg`), no por
# moneda. Es deliberado: desde que `position_currency` reconoce como USD también a
# las especies …C, filtrar por moneda metía en la curva la pata MEP **y** la CABLE
# de cada bono → dos puntos casi superpuestos por bono, con peso doble en el ajuste
# logarítmico frente a los sólo-MEP (AO27D/AO28D). La curva USD del panel es la de
# MEP (dólar bolsa), que es la que cotiza el mercado local.
_CURVA_GROUPS = {
    "soberanos_usd": ("Soberanos USD", {"BONAR", "GLOBAL", "BOPREAL"}, "MEP"),
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
    label, types, fx_leg = _CURVA_GROUPS[grupo]
    metrics = []
    for m in state.metrics():
        inst = m.snapshot.instrument if m.snapshot else None
        if not inst or inst.instrument_type not in types:
            continue
        if m.duration is None or m.duration <= 0 or m.tir is None:
            continue
        if fx_leg and position_fx_leg(inst.instrument_type, inst.ticker) != fx_leg:
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
