"""Router de paneles de bonos (HTMX SSR).

`GET /` → página index con los 6 paneles de bonos.
`GET /panels/{id}/rows` → fragmento <tbody> que HTMX refresca cada 5s.

Reemplaza el polling global de `/api/snapshot` + el render JS de `app.js` por
fragmentos server-side. Reusa los InstrumentMetrics ya calculados en AppState
(motor Fase 1) y el column-schema del http.server original.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from apps.web.deps import get_state
from core.domain.portfolio import position_currency
from core.domain.services import FinancialEngine

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# --- Column schemas (espejo de server._get_columns para los paneles de bonos) --- #
_BONARES_COLS = [
    {"key": "ticker", "label": "Ticker", "kind": "text"},
    {"key": "vto", "label": "Vto", "kind": "date"},
    {"key": "days_next_coupon", "label": "Próx Cup", "kind": "number", "decimals": 0},
    {"key": "price", "label": "Precio", "kind": "number", "decimals": 2},
    {"key": "technical_value", "label": "V.Téc", "kind": "number", "decimals": 2},
    {"key": "parity", "label": "Paridad", "kind": "percent", "decimals": 2},
    {"key": "tir", "label": "TIR", "kind": "percent", "decimals": 2},
    {"key": "duration", "label": "MD", "kind": "number", "decimals": 2},
    {"key": "change_pct", "label": "%Día", "kind": "percent_signed", "decimals": 2},
    {"key": "volume", "label": "Vol $", "kind": "volume"},
]
_CER_COLS = [
    {"key": "ticker", "label": "Ticker", "kind": "text"},
    {"key": "category", "label": "Categoría", "kind": "text"},
    {"key": "vto", "label": "Vto", "kind": "date"},
    {"key": "days_next_coupon", "label": "Próx Cup", "kind": "number", "decimals": 0},
    {"key": "price", "label": "Precio", "kind": "number", "decimals": 2},
    {"key": "technical_value", "label": "V.Téc", "kind": "number", "decimals": 2},
    {"key": "parity", "label": "Paridad", "kind": "percent", "decimals": 2},
    {"key": "tir", "label": "TIR", "kind": "percent", "decimals": 2},
    {"key": "duration", "label": "DM", "kind": "number", "decimals": 2},
    {"key": "change_pct", "label": "Var%", "kind": "percent_signed", "decimals": 2},
    {"key": "volume", "label": "Vol $", "kind": "volume"},
]
_TASA_FIJA_COLS = [
    {"key": "ticker", "label": "Ticker", "kind": "text"},
    {"key": "dias", "label": "Días", "kind": "number", "decimals": 0},
    {"key": "price", "label": "Precio", "kind": "number", "decimals": 2},
    {"key": "technical_value", "label": "V.Téc", "kind": "number", "decimals": 2},
    {"key": "parity", "label": "Paridad", "kind": "percent", "decimals": 2},
    {"key": "tir", "label": "TIR/TEA", "kind": "percent", "decimals": 2},
    {"key": "tna", "label": "TNA(365)", "kind": "percent", "decimals": 2},
    {"key": "tem", "label": "TEM(365)", "kind": "percent", "decimals": 2},
    {"key": "duration", "label": "DM", "kind": "number", "decimals": 2},
    {"key": "change_pct", "label": "Var %", "kind": "percent_signed", "decimals": 2},
    {"key": "volume", "label": "Vol $", "kind": "volume"},
]
_TAMAR_COLS = [
    {"key": "ticker", "label": "Ticker", "kind": "text"},
    {"key": "vto", "label": "Vto", "kind": "date"},
    {"key": "dias", "label": "Días", "kind": "number", "decimals": 0},
    {"key": "price", "label": "Precio", "kind": "number", "decimals": 2},
    {"key": "tir", "label": "TIR (TEA)", "kind": "percent", "decimals": 2},
    {"key": "duration", "label": "MD", "kind": "number", "decimals": 2},
    {"key": "change_pct", "label": "%Día", "kind": "percent_signed", "decimals": 2},
    {"key": "volume", "label": "Vol $", "kind": "volume"},
]

_VR_COLS = [
    {"key": "ticker", "label": "Ticker", "kind": "text"},
    {"key": "grupo", "label": "Tipo", "kind": "text"},
    {"key": "duration", "label": "MD", "kind": "number", "decimals": 2},
    {"key": "tir", "label": "TIR", "kind": "percent", "decimals": 2},
    {"key": "spread_curva", "label": "vs curva", "kind": "percent_signed", "decimals": 2},
    {"key": "carry_roll", "label": "C+R 30d", "kind": "percent_signed", "decimals": 2},
]

# id -> (título, {instrument_types}, columnas)
PANELS = {
    "bonares": ("BONARES Y GLOBALES", {"BONAR", "GLOBAL"}, _BONARES_COLS),
    "bopreales": ("BOPREALES", {"BOPREAL"}, _BONARES_COLS),
    "cer": ("BONOS CER", {"CER", "LECER", "BONCER", "BONCER ZC", "CON CUPON", "STEP-UP"}, _CER_COLS),
    "tasa_fija": ("TASA FIJA", {"LECAP", "BONCAP", "BONOFIJA"}, _TASA_FIJA_COLS),
    "dolar_linked": ("DOLAR LINKED", {"DOLAR_LINKED"}, _BONARES_COLS),
    "tamar": ("TAMAR / DUAL", {"PURO", "DUAL", "DUAL_CER_TAMAR"}, _TAMAR_COLS),
    "valor_relativo": ("VALOR RELATIVO · rich / cheap (curvas peso)", set(), _VR_COLS),
}
PANEL_ORDER = ["bonares", "cer", "tasa_fija", "tamar", "dolar_linked", "bopreales", "valor_relativo"]

# Grupos para el ajuste de curva log (TIR = a + b·ln(MD)) — un fit por grupo.
# Sólo curvas peso de flavor único (Tasa Fija nominal, CER real): los soberanos
# hard-dollar conviven en 3 flavors (peso/MEP/cable) con escalas de precio/TIR
# distintas, y mezclarlos da rich/cheap sin sentido. El rich/cheap de soberanos
# requiere la metodología exacta del server (universo curado) — pendiente.
_RV_GROUPS = {
    "Tasa Fija": {"LECAP", "BONCAP", "BONOFIJA"},
    "CER": {"CER", "LECER", "BONCER", "BONCER ZC", "CON CUPON", "STEP-UP"},
}
_ONE_MONTH_YEARS = 1.0 / 12.0


def _fit_log_curve(metrics) -> Optional[tuple]:
    """TIR = a + b·ln(MD) sobre los (MD, TIR) del grupo. None si <3 puntos."""
    pairs = [(m.duration, m.tir) for m in metrics
             if m.duration and m.duration > 0 and m.tir is not None]
    if len(pairs) < 3:
        return None
    try:
        ln_dm = np.array([math.log(d) for d, _ in pairs])
        tirs = np.array([t for _, t in pairs])
        b, a = np.polyfit(ln_dm, tirs, 1)
        return float(a), float(b)
    except Exception:
        return None


def _spread_carry(m, fit) -> tuple:
    """(spread_curva, carry_roll) en decimales. spread = TIR − TIR_curva."""
    if fit is None or not m.duration or m.duration <= 0 or m.tir is None:
        return None, None
    a, b = fit
    try:
        tir_fitted = a + b * math.log(m.duration)
        spread = m.tir - tir_fitted
        carry = None
        tem = FinancialEngine.tea_to_tem(m.tir)
        dm_rolled = m.duration - _ONE_MONTH_YEARS
        if tem is not None and dm_rolled > 0.001:
            tir_rolled = a + b * math.log(dm_rolled)
            roll_down = -m.duration * (tir_rolled - m.tir)
            carry = tem + roll_down
        return spread, carry
    except Exception:
        return None, None


def _rv_map(state) -> dict:
    """{ticker: {"grupo", "spread"(%u), "carry"(%u)}} vía fit log por (grupo, moneda).

    Bucketea por moneda: mezclar globals USD (~9%) con soberanos ARS (~60%) en
    una sola curva da spreads sin sentido. Cada curva se ajusta sobre un universo
    de rendimiento homogéneo.
    """
    by_bucket: dict = {}
    for m in state.metrics():
        inst = m.snapshot.instrument if m.snapshot else None
        if not inst:
            continue
        for label, types in _RV_GROUPS.items():
            if inst.instrument_type in types:
                ccy = position_currency(inst.instrument_type, inst.ticker)
                by_bucket.setdefault((label, ccy), []).append(m)
                break
    out: dict = {}
    for (label, _ccy), ms in by_bucket.items():
        fit = _fit_log_curve(ms)
        for m in ms:
            sp, ca = _spread_carry(m, fit)
            out[m.snapshot.instrument.ticker] = {
                "grupo": label,
                "spread": sp * 100 if sp is not None else None,
                "carry": ca * 100 if ca is not None else None,
            }
    return out


def _next_coupon_date(inst, today: date) -> Optional[date]:
    for cf in sorted(inst.cashflows or [], key=lambda c: c.date):
        if cf.date > today and cf.interest > 0:
            return cf.date
    return None


def _pct(v: Optional[float]) -> Optional[float]:
    return v * 100 if v is not None else None


def _row_values(m, today: date) -> dict:
    """Espejo de server._base_bond_row (subset usado por los paneles de bonos)."""
    inst = m.snapshot.instrument
    vto = inst.maturity_date
    next_cp = _next_coupon_date(inst, today)
    return {
        "ticker": inst.ticker,
        "category": inst.category,
        "vto": vto,
        "days_next_coupon": (next_cp - today).days if next_cp else None,
        "dias": (vto - today).days if vto else None,
        "price": m.snapshot.price,
        "technical_value": m.technical_value,
        "parity": _pct(m.parity),
        "tir": _pct(m.tir),
        "tna": _pct(FinancialEngine.tea_to_tna(m.tir)),
        "tem": _pct(FinancialEngine.tea_to_tem(m.tir)),
        "duration": m.duration,
        "change_pct": m.snapshot.change_pct,
        "volume": m.snapshot.volume,
    }


def _fmt(value, kind: str, decimals: int = 2) -> str:
    if value is None or value == "":
        return "—"
    try:
        if kind == "text":
            return str(value)
        if kind == "date":
            return value.strftime("%d/%m/%y") if hasattr(value, "strftime") else str(value)
        if kind == "number":
            return f"{float(value):,.{decimals}f}"
        if kind == "percent":
            return f"{float(value):.{decimals}f}%"
        if kind == "percent_signed":
            return f"{float(value):+.{decimals}f}%"
        if kind == "volume":
            v = float(value)
            for unit, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
                if abs(v) >= unit:
                    return f"{v / unit:.1f}{suf}"
            return f"{v:.0f}"
    except (TypeError, ValueError):
        return str(value)
    return str(value)


def _cell_class(value, kind: str) -> str:
    if kind == "percent_signed" and value is not None:
        try:
            f = float(value)
            return "pos" if f > 0 else ("neg" if f < 0 else "")
        except (TypeError, ValueError):
            return ""
    if kind in ("number", "percent", "percent_signed", "volume"):
        return "num"
    return ""


def _build_rv_rows(state) -> List[dict]:
    """Filas del panel valor_relativo: todos los bonos con su spread vs curva
    (por grupo), ordenados del más barato (spread > 0) al más caro."""
    rv = _rv_map(state)
    by_ticker = {m.snapshot.instrument.ticker: m for m in state.metrics()
                 if m.snapshot and m.snapshot.instrument}
    rows = []
    for tk, info in rv.items():
        if info["spread"] is None:
            continue
        m = by_ticker.get(tk)
        if m is None:
            continue
        raw = {
            "ticker": tk, "grupo": info["grupo"], "duration": m.duration,
            "tir": _pct(m.tir), "spread_curva": info["spread"], "carry_roll": info["carry"],
        }
        cells = [{"text": _fmt(raw[c["key"]], c["kind"], c.get("decimals", 2)),
                  "cls": _cell_class(raw[c["key"]], c["kind"])} for c in _VR_COLS]
        rows.append({"ticker": tk, "cells": cells, "_spread": info["spread"]})
    rows.sort(key=lambda r: r["_spread"], reverse=True)
    return rows


def _build_rows(panel_id: str, state) -> List[dict]:
    if panel_id == "valor_relativo":
        return _build_rv_rows(state)
    if panel_id not in PANELS:
        return []
    _title, types, cols = PANELS[panel_id]
    today = date.today()
    metrics = [m for m in state.metrics()
               if m.snapshot and m.snapshot.instrument and m.snapshot.instrument.instrument_type in types]
    metrics.sort(key=lambda m: (m.duration is None, m.duration or 0.0))
    rows = []
    for m in metrics:
        vals = _row_values(m, today)
        cells = []
        for c in cols:
            raw = vals.get(c["key"])
            cells.append({
                "text": _fmt(raw, c["kind"], c.get("decimals", 2)),
                "cls": _cell_class(raw, c["kind"]),
            })
        rows.append({"ticker": vals["ticker"], "cells": cells})
    return rows


@router.get("/", response_class=HTMLResponse)
def index(request: Request, state=Depends(get_state)):
    panels = [{"id": pid, "title": PANELS[pid][0], "columns": PANELS[pid][2],
               "rows": _build_rows(pid, state)} for pid in PANEL_ORDER]
    return _TEMPLATES.TemplateResponse(
        request, "pages/index.html",
        {"panels": panels, "last_refresh": state.last_refresh},
    )


@router.get("/panels/{panel_id}/rows", response_class=HTMLResponse)
def panel_rows(panel_id: str, request: Request, state=Depends(get_state)):
    cols = PANELS.get(panel_id, (None, None, []))[2]
    return _TEMPLATES.TemplateResponse(
        request, "fragments/panel_rows.html",
        {"rows": _build_rows(panel_id, state), "ncols": len(cols)},
    )
