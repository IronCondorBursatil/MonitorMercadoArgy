"""Router de paneles de bonos (HTMX SSR).

`GET /` → página index con los 6 paneles de bonos.
`GET /panels/{id}/rows` → fragmento <tbody> que HTMX refresca cada 5s.

Reemplaza el polling global de `/api/snapshot` + el render JS de `app.js` por
fragmentos server-side. Reusa los InstrumentMetrics ya calculados en AppState
(motor Fase 1) y el column-schema del http.server original.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from apps.web.deps import get_state
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

# id -> (título, {instrument_types}, columnas)
PANELS = {
    "bonares": ("BONARES Y GLOBALES", {"BONAR", "GLOBAL"}, _BONARES_COLS),
    "bopreales": ("BOPREALES", {"BOPREAL"}, _BONARES_COLS),
    "cer": ("BONOS CER", {"CER", "LECER", "BONCER", "BONCER ZC", "CON CUPON", "STEP-UP"}, _CER_COLS),
    "tasa_fija": ("TASA FIJA", {"LECAP", "BONCAP", "BONOFIJA"}, _TASA_FIJA_COLS),
    "dolar_linked": ("DOLAR LINKED", {"DOLAR_LINKED"}, _BONARES_COLS),
    "tamar": ("TAMAR / DUAL", {"PURO", "DUAL", "DUAL_CER_TAMAR"}, _TAMAR_COLS),
}
PANEL_ORDER = ["bonares", "cer", "tasa_fija", "tamar", "dolar_linked", "bopreales"]


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


def _build_rows(panel_id: str, state) -> List[dict]:
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
