"""Router de paneles de bonos (HTMX SSR).

`GET /` → página index con los 6 paneles de bonos.
`GET /panels/{id}/rows` → fragmento <tbody> que HTMX refresca cada 5s.

Reemplaza el polling global de `/api/snapshot` + el render JS de `app.js` por
fragmentos server-side. Reusa los InstrumentMetrics ya calculados en AppState
(motor Fase 1) y el column-schema del http.server original.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import date
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from apps.web.deps import get_fx, get_indices, get_provider, get_rofex, get_state
from config.settings import settings
from core.holiday_engine import settlement_byma_date
from core.domain.instrument_groups import OBLIGACIONES_NEGOCIABLES, PANEL_LIDER
from core.domain.portfolio import position_currency
from core.domain.services import FinancialEngine
from core.infrastructure.futures_provider import (
    DEFAULT_SYMBOLS as ROFEX_SYMBOLS,
    implied_tna,
    parse_contract_maturity,
    resolve_spot_for_tna,
)

logger = logging.getLogger(__name__)
router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Layout por defecto del dashboard (posiciones + paneles cerrados + columnas).
# Se guarda junto a la .db (fuera de OneDrive); si no existe, el front usa el
# auto-layout. El usuario lo setea con "Guardar como default" en el menú CONFIG.
_LAYOUT_FILE = str(Path(str(settings.catalog_db)).parent / "dashboard_layout.json")


def _json_for_script(obj) -> str:
    """json.dumps seguro para embeber en un <script>: escapa < > & y los separadores
    de línea U+2028/U+2029 a \\uXXXX. Siguen siendo JSON válido (parsean al mismo
    valor) pero ya no pueden cerrar el tag ni romper el parser JS (mitiga XSS, S4)."""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace(" ", "\\u2028").replace(" ", "\\u2029"))


def _read_default_layout() -> str:
    """JSON del layout default escapado para embeber en <script>, o 'null'. Se
    re-serializa (no se devuelve el archivo crudo) para neutralizar un payload
    malicioso que un POST a /panels/layout pudiera haber guardado."""
    try:
        with open(_LAYOUT_FILE, "r", encoding="utf-8") as f:
            obj = json.loads(f.read())
        return _json_for_script(obj)
    except (OSError, ValueError):
        return "null"

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
# Soberanos USD (Bonares/Globales) + Bopreales: igual que _BONARES_COLS pero con
# las ventanas de rendimiento (Sem/1M/3M/YTD/1A) tras %Día, alimentadas por el
# store de precios (price_history.py: Data912 historical + acumulación del feed).
# Dólar Linked sigue usando _BONARES_COLS (su histórico recién acumula con el tiempo).
_SOBERANO_USD_COLS = [
    *_BONARES_COLS[:-1],  # Ticker…%Día (todo menos "Vol $")
    {"key": "var_7d", "label": "Sem", "kind": "percent_signed", "decimals": 2},
    {"key": "var_30d", "label": "1M", "kind": "percent_signed", "decimals": 2},
    {"key": "var_90d", "label": "3M", "kind": "percent_signed", "decimals": 2},
    {"key": "var_ytd", "label": "YTD", "kind": "percent_signed", "decimals": 2},
    {"key": "var_1y", "label": "1A", "kind": "percent_signed", "decimals": 2},
    _BONARES_COLS[-1],    # "Vol $" al final
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
_ON_COLS = [
    {"key": "ticker", "label": "Ticker", "kind": "text"},
    {"key": "short_name", "label": "Emisor", "kind": "text"},
    {"key": "vto", "label": "Vto", "kind": "date"},
    {"key": "price", "label": "Precio", "kind": "number", "decimals": 2},
    {"key": "technical_value", "label": "V.Téc", "kind": "number", "decimals": 2},
    {"key": "parity", "label": "Paridad", "kind": "percent", "decimals": 2},
    {"key": "tir", "label": "TIR", "kind": "percent", "decimals": 2},
    {"key": "duration", "label": "MD", "kind": "number", "decimals": 2},
    {"key": "change_pct", "label": "%Día", "kind": "percent_signed", "decimals": 2},
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
_PANEL_LIDER_COLS = [
    {"key": "ticker", "label": "Ticker", "kind": "text"},
    {"key": "bid", "label": "Compra", "kind": "number", "decimals": 2},
    {"key": "ask", "label": "Venta", "kind": "number", "decimals": 2},
    {"key": "mid", "label": "Mid", "kind": "number", "decimals": 2},
    {"key": "change_pct", "label": "Día %", "kind": "percent_signed", "decimals": 2},
    {"key": "volume", "label": "Vol $", "kind": "volume"},
    {"key": "operations", "label": "Ops", "kind": "number", "decimals": 0},
]
_FUTUROS_COLS = [
    {"key": "ticker", "label": "Contrato", "kind": "text"},
    {"key": "vto", "label": "Vto", "kind": "date"},
    {"key": "bid", "label": "Compra", "kind": "number", "decimals": 2},
    {"key": "ask", "label": "Venta", "kind": "number", "decimals": 2},
    {"key": "last", "label": "Último", "kind": "number", "decimals": 2},
    {"key": "settle", "label": "Ajuste", "kind": "number", "decimals": 2},
    {"key": "tna", "label": "TNA", "kind": "percent", "decimals": 2},
    {"key": "open_interest", "label": "OP.INT", "kind": "volume"},
    {"key": "volume", "label": "Vol", "kind": "volume"},
]
_BEI_TENOR_COLS = [
    {"key": "plazo", "label": "Plazo", "kind": "text"},
    {"key": "dias", "label": "Días", "kind": "number", "decimals": 0},
    {"key": "tea_nominal", "label": "TEA Nom", "kind": "percent", "decimals": 2},
    {"key": "tea_real", "label": "TEA Real", "kind": "percent", "decimals": 2},
    {"key": "tamar_fwd", "label": "TAMAR fwd", "kind": "percent", "decimals": 2},
    {"key": "bei_spot", "label": "BEI spot", "kind": "percent", "decimals": 2},
    {"key": "bei_fwd", "label": "BEI fwd", "kind": "percent", "decimals": 2},
    {"key": "bei_g_adj", "label": "BEI γ-adj", "kind": "percent", "decimals": 2},
    {"key": "bei_tamar", "label": "BEI TAMAR", "kind": "percent", "decimals": 2},
    {"key": "dev_implicita", "label": "Deval DLR", "kind": "percent", "decimals": 2},
    {"key": "tc_real", "label": "TC real", "kind": "percent_signed", "decimals": 2},
]
_BEI_SENDERO_COLS = [
    {"key": "mes", "label": "Mes", "kind": "text"},
    {"key": "dias_mes", "label": "Días", "kind": "number", "decimals": 0},
    {"key": "bei_mensual", "label": "BEI mensual", "kind": "percent", "decimals": 2},
    {"key": "rem_mensual", "label": "REM mensual", "kind": "percent", "decimals": 2},
    {"key": "diff", "label": "BEI − REM", "kind": "percent_signed", "decimals": 2},
]
_BEI_PARES_COLS = [
    {"key": "lecap", "label": "LECAP", "kind": "text"},
    {"key": "boncer", "label": "BONCER", "kind": "text"},
    {"key": "vto_lecap", "label": "Vto LECAP", "kind": "date"},
    {"key": "vto_cer", "label": "Vto CER", "kind": "date"},
    {"key": "dias", "label": "Días", "kind": "number", "decimals": 0},
    {"key": "delta_m1", "label": "δ − 1", "kind": "percent", "decimals": 2},
    {"key": "infl_mensual_impl", "label": "Infl mes impl.", "kind": "percent", "decimals": 2},
]
_BEI_TABLE_KEY = {"bei_tenor": "tenor", "bei_sendero": "sendero", "bei_pares": "pares"}

# id -> (título, {instrument_types}, columnas)
PANELS = {
    "bonares": ("BONARES Y GLOBALES", {"BONAR", "GLOBAL"}, _SOBERANO_USD_COLS),
    "bopreales": ("BOPREALES", {"BOPREAL"}, _SOBERANO_USD_COLS),
    "cer": ("BONOS CER", {"CER", "LECER", "BONCER", "BONCER ZC", "CON CUPON", "STEP-UP"}, _CER_COLS),
    "tasa_fija": ("TASA FIJA", {"LECAP", "BONCAP", "BONOFIJA"}, _TASA_FIJA_COLS),
    "dolar_linked": ("DOLAR LINKED", {"DOLAR_LINKED"}, _BONARES_COLS),
    "tamar": ("TAMAR / DUAL", {"PURO", "DUAL", "DUAL_CER_TAMAR"}, _TAMAR_COLS),
    "obligaciones_negociables": ("OBLIGACIONES NEGOCIABLES · ON USD", set(OBLIGACIONES_NEGOCIABLES), _ON_COLS),
    "valor_relativo": ("VALOR RELATIVO · rich / cheap (curvas peso)", set(), _VR_COLS),
    "panel_lider": ("PANEL LÍDER · acciones", set(), _PANEL_LIDER_COLS),
    "futuros": ("FUTUROS DLR (Matba/Rofex)", set(), _FUTUROS_COLS),
    "bei_tenor": ("BEI POR TENOR (NSS + Fisher)", set(), _BEI_TENOR_COLS),
    "bei_sendero": ("SENDERO MENSUAL · BEI vs REM-BCRA", set(), _BEI_SENDERO_COLS),
    "bei_pares": ("MÉTODO DE PARES (cross-check NT8 §A)", set(), _BEI_PARES_COLS),
}
PANEL_ORDER = ["bonares", "cer", "tasa_fija", "tamar", "dolar_linked", "bopreales",
               "obligaciones_negociables",
               "valor_relativo", "panel_lider", "futuros",
               "bei_tenor", "bei_sendero", "bei_pares"]

# Paneles cuyas especies cotizan en 3 monedas (mismo bono): se les agrega el filtro
# ARS/MEP/CABLE en el header (default MEP). La moneda se deriva del sufijo del ticker
# (D=MEP, C=CABLE, resto=ARS). BOPREALes incluidos: cotizan en pesos (base BPO*),
# MEP (…D) y cable (…C) — la pata pesos se linkea por ISIN (ver backfill_legs_from_universe).
CCY_FILTER_PANELS = {"bonares", "obligaciones_negociables", "bopreales"}

# Paneles con selector de plazo de liquidación CI (T+0) / 24hs (T+1). El precio y
# todo lo que deriva de él (TIR/paridad/MD/V.Téc) se recalcula on-demand para el
# plazo elegido desde el snapshot CI del hub (BYMA trae ambos plazos en una llamada).
# BONARES (soberanos USD, 3 monedas) y CER (peso ajustado: el plazo además mueve el
# ref de indexación CER de la V.Téc, no solo el descuento — ver _ci_metrics/settle_lag).
SETTLE_FILTER_PANELS = {"bonares", "cer"}

# Paneles donde se resalta la columna TIR (fondo accent, mismo efecto que la
# `sortcol` del FCI) para distinguirla rápido: TODOS los paneles de bonos que
# tienen columna TIR — soberanos (CER / Tasa Fija / TAMAR / Dólar Linked / Bonares
# / Bopreales) + corporativos (ON) + valor relativo. Se aplica en el panel del
# dashboard y en el popup de compartir (la foto).
_TIR_HL_PANELS = {pid for pid, (_t, _types, _cols) in PANELS.items()
                  if any(c.get("key") == "tir" for c in _cols)}
# Columna a resaltar (fondo accent) por panel — en el dashboard Y en la foto. Por defecto
# la TIR (soberanos/CER/ON/DL/TAMAR/VR…), pero TASA FIJA resalta la TNA: son instrumentos
# a tasa fija (en general <1 año) que se comparan por tasa devengada nominal (TNA), no por
# TIR. Panel sin entrada → sin resalte.
_HL_COL_KEY = {pid: "tir" for pid in _TIR_HL_PANELS}
_HL_COL_KEY["tasa_fija"] = "tna"


def _ticker_ccy(ticker: str) -> str:
    """Moneda de una especie soberana por su sufijo: D=MEP, C=CABLE, resto=ARS."""
    t = (ticker or "").upper()
    if t.endswith("D"):
        return "MEP"
    if t.endswith("C"):
        return "CABLE"
    return "ARS"

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
        "short_name": inst.short_name,
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
        # Rendimientos por ventana (fracción → %). Salen de fetch_historical_prices
        # (store price_history.py + CSV legacy), poblados por ticker a medida que hay
        # histórico — completos para soberanos/CER (priming Data912), el resto acumula.
        "var_7d": _pct(m.variance_7d),
        "var_30d": _pct(m.variance_30d),
        "var_90d": _pct(m.variance_90d),
        "var_ytd": _pct(m.variance_ytd),
        "var_1y": _pct(m.variance_1y),
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


def _build_panel_lider_rows(provider) -> List[dict]:
    """Acciones del Panel Líder desde el cache de Data912 (/arg_stocks vía
    fetch_snapshots). No tocan el repo de bonos → no son clickables al popup."""
    if provider is None:
        return []
    try:
        snaps = provider.fetch_snapshots(PANEL_LIDER)
    except Exception:
        return []
    rows = []
    for tk in PANEL_LIDER:
        s = snaps.get(tk)
        if s is None:
            continue
        mid = ((s.bid + s.ask) / 2.0) if (s.bid and s.ask) else (s.price or None)
        raw = {"ticker": tk, "bid": s.bid, "ask": s.ask, "mid": mid,
               "change_pct": s.change_pct, "volume": s.volume, "operations": s.operations}
        cells = [{"text": _fmt(raw[c["key"]], c["kind"], c.get("decimals", 2)),
                  "cls": _cell_class(raw[c["key"]], c["kind"])} for c in _PANEL_LIDER_COLS]
        rows.append({"ticker": tk, "cells": cells, "clickable": False})
    return rows


def _build_futuros_rows(rofex, fx, bcra) -> List[dict]:
    """Curva DLR Rofex/Matba: TNA implícita = (futuro_last/spot)^(365/d) − 1.
    Reusa los helpers de futures_provider (spot híbrido fx/A3500, parse vto)."""
    if rofex is None:
        return []
    try:
        quotes = rofex.get_quotes(ROFEX_SYMBOLS)
        spot = resolve_spot_for_tna(fx, bcra)
    except Exception:
        return []
    rows = []
    for sym in ROFEX_SYMBOLS:
        q = quotes.get(sym)
        if not q:
            continue
        mat = parse_contract_maturity(sym)
        last = q.get("last")
        tna = implied_tna(last, spot, mat) if (last and spot and mat) else None
        raw = {
            "ticker": sym, "vto": mat, "bid": q.get("bid"), "ask": q.get("ask"),
            "last": last, "settle": q.get("settle"),
            "tna": tna * 100 if tna is not None else None,
            "open_interest": q.get("open_interest"), "volume": q.get("volume"),
        }
        cells = [{"text": _fmt(raw[c["key"]], c["kind"], c.get("decimals", 2)),
                  "cls": _cell_class(raw[c["key"]], c["kind"])} for c in _FUTUROS_COLS]
        rows.append({"ticker": sym, "cells": cells, "clickable": False})
    return rows


# --- Futuros: popup tabbed (Curva Rofex / Carry / Cobertura) ----------------- #
# El popup "foto" del panel de futuros NO es la foto genérica (tabla + scatter
# TIR×MD): los futuros no tienen MD/TIR. Es una vista propia con la curva de TNA
# por contrato + métricas de derivados de dólar (TNA/TEA lineal·compuesta, crawl
# mensual, Var 1D vs ajuste previo, basis vs spot, y carry vs la curva de tasa
# fija en pesos). Ver fragments/futuros_share.html.
_TASA_FIJA_TYPES = {"LECAP", "BONCAP", "BONOFIJA"}


def _implied_rates(fx_ref: Optional[float], spot: Optional[float],
                   days: Optional[int]) -> tuple:
    """(tna_lineal, tea, crawl_mensual) en decimales desde futuro/spot/días.

    - TNA lineal (base 365):   (F/S − 1) · 365/d   — la "TNA" del informe A3.
    - TEA   (efectiva anual):  (F/S)^(365/d) − 1
    - Crawl mensual (compuesto):(F/S)^(30/d) − 1
    (None, None, None) si falta algún input o hay overflow."""
    if not (fx_ref and spot and spot > 0 and days and days > 0):
        return None, None, None
    ratio = fx_ref / spot
    if ratio <= 0:
        return None, None, None
    try:
        tna = (ratio - 1.0) * 365.0 / days
        tea = ratio ** (365.0 / days) - 1.0
        crawl = ratio ** (30.0 / days) - 1.0
        return tna, tea, crawl
    except (ValueError, OverflowError, ZeroDivisionError):
        return None, None, None


def _peso_tea_curve(state, today: date):
    """Interpolador días→TEA de la curva de tasa fija en pesos (LECAP/BONCAP/BONOFIJA),
    para el carry de la pestaña 'Carry'. Lineal por días, extrapolación plana en los
    extremos. None si hay <2 puntos (sin curva → la columna carry queda en '—')."""
    pts = []
    for m in state.metrics():
        inst = m.snapshot.instrument if m.snapshot else None
        if not inst or inst.instrument_type not in _TASA_FIJA_TYPES:
            continue
        if m.tir is None or not inst.maturity_date:
            continue
        d = (inst.maturity_date - today).days
        if d > 0:
            pts.append((d, m.tir))
    if len(pts) < 2:
        return None
    pts.sort()
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]

    def interp(d: int) -> float:
        if d <= xs[0]:
            return ys[0]
        if d >= xs[-1]:
            return ys[-1]
        for i in range(1, len(xs)):
            if xs[i] >= d:
                x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
                f = (d - x0) / (x1 - x0) if x1 != x0 else 0.0
                return y0 + (y1 - y0) * f
        return ys[-1]

    return interp


def _futuros_label(sym: str) -> str:
    """'DLR/JUN26' → 'jun-26' (sufijo en minúsculas con guion)."""
    suf = (sym.split("/", 1)[1] if "/" in sym else sym).rstrip("M")
    if len(suf) >= 5:
        return f"{suf[:3].lower()}-{suf[3:5]}"
    return sym


def _build_futuros_share(rofex, fx, indices, state) -> dict:
    """Payload del popup tabbed de Futuros: por contrato, todas las métricas de
    derivados de dólar + spot + flag de curva peso. {} si falla (degrada solo)."""
    if rofex is None:
        return {}
    try:
        quotes = rofex.get_quotes(ROFEX_SYMBOLS)
        spot = resolve_spot_for_tna(fx, indices)
    except Exception:
        return {}
    today = date.today()
    peso_curve = _peso_tea_curve(state, today) if state is not None else None
    contracts: List[dict] = []
    for sym in ROFEX_SYMBOLS:
        q = quotes.get(sym)
        if not q:
            continue
        mat = parse_contract_maturity(sym)
        days = (mat - today).days if mat else None
        last, settle, prev = q.get("last"), q.get("settle"), q.get("prev_settle")
        fx_ref = last if last is not None else settle    # precio de referencia (= panel)
        if not (fx_ref and days and days > 0):
            continue
        tna, tea, crawl = _implied_rates(fx_ref, spot, days)
        var1d = (fx_ref / prev - 1.0) * 100 if (fx_ref and prev) else None
        peso_tea = peso_curve(days) if peso_curve else None
        carry = (peso_tea - tea) * 100 if (peso_tea is not None and tea is not None) else None
        contracts.append({
            "label": _futuros_label(sym),
            "fx": fx_ref,
            "var1d": var1d,
            "tna": _pct(tna), "tea": _pct(tea), "crawl": _pct(crawl),
            "peso_tea": _pct(peso_tea), "carry": carry,
            "oi": q.get("open_interest"), "vol": q.get("volume"),
            "dias": days,
            "basis": (fx_ref - spot) if spot else None,
            "vs_spot": (fx_ref / spot - 1.0) * 100 if spot else None,
        })
    return {"contracts": contracts, "spot": spot,
            "has_peso_curve": peso_curve is not None}


def _build_bei_rows(panel_id: str, state) -> List[dict]:
    """Filas de los paneles BEI desde las tablas de compute_bei_tables (AppState).
    Los valores vienen en decimales → las columnas percent se escalan ×100."""
    tables = state.bei_tables()
    if not tables:
        return []
    cols = PANELS[panel_id][2]
    rows = []
    for r in tables.get(_BEI_TABLE_KEY[panel_id], []):
        cells = []
        for c in cols:
            raw = r.get(c["key"])
            if c["kind"] in ("percent", "percent_signed") and raw is not None:
                raw = raw * 100
            cells.append({"text": _fmt(raw, c["kind"], c.get("decimals", 2)),
                          "cls": _cell_class(raw, c["kind"])})
        rows.append({"ticker": r.get(cols[0]["key"]), "cells": cells, "clickable": False})
    return rows


def _ci_metrics(panel_id: str, request: Request, hist_provider) -> Optional[list]:
    """Métricas on-demand del plazo CI (T+0): corre el motor para los tipos del panel
    leyendo el snapshot CI del hub. Todo en memoria (el hub ya tiene CI del refresh,
    sin red extra). None si el panel no tiene tipos (no aplica)."""
    from core.infrastructure.provider_hub import HubMarketDataProvider
    from core.use_cases.generate_report import GenerateMonitorReport
    from apps.web.deps import get_repo

    types = PANELS[panel_id][1]
    if not types:
        return None
    hub = request.app.state.hub
    ci_provider = HubMarketDataProvider(hub, hist_provider, settle="CI")
    # CI (T+0): settle_date=hoy descuenta TIR/MD desde hoy; settle_lag=0 mueve el ref
    # CER de la V.Téc a T+0 también → todo el cálculo en consonancia con el precio CI.
    return GenerateMonitorReport(get_repo(), ci_provider).execute(
        list(types), settle_date=date.today(), settle_lag=0)


def _build_rows(panel_id: str, state, provider=None, cols_override=None,
                metrics_override=None) -> List[dict]:
    if panel_id == "valor_relativo":
        return _build_rv_rows(state)
    if panel_id == "panel_lider":
        return _build_panel_lider_rows(provider)
    if panel_id in _BEI_TABLE_KEY:
        return _build_bei_rows(panel_id, state)
    if panel_id not in PANELS:
        return []
    _title, types, cols = PANELS[panel_id]
    if cols_override is not None:      # popup "compartir": fila con TODAS las columnas calculadas
        cols = cols_override
    today = date.today()
    ccy_filterable = panel_id in CCY_FILTER_PANELS
    # metrics_override: métricas ya calculadas (p.ej. plazo CI on-demand) — ya son de
    # los tipos del panel. Si no, se filtran las de AppState (plazo 24hs, default).
    if metrics_override is not None:
        metrics = [m for m in metrics_override
                   if m.snapshot and m.snapshot.instrument]
    else:
        metrics = [m for m in state.metrics()
                   if m.snapshot and m.snapshot.instrument and m.snapshot.instrument.instrument_type in types]
    metrics.sort(key=lambda m: (m.duration is None, m.duration or 0.0))
    rows = []
    for m in metrics:
        vals = _row_values(m, today)
        ccy = _ticker_ccy(vals["ticker"]) if ccy_filterable else None
        cells = []
        hl_key = _HL_COL_KEY.get(panel_id)
        for c in cols:
            raw = vals.get(c["key"])
            dec = c.get("decimals", 2)
            # Precio en pesos de la pata ARS de un soberano: sin decimales (91,990).
            if c["key"] == "price" and ccy == "ARS":
                dec = 0
            cls = _cell_class(raw, c["kind"])
            if hl_key and c["key"] == hl_key:
                cls = (cls + " tircol").strip()
            cells.append({
                "text": _fmt(raw, c["kind"], dec),
                "cls": cls,
            })
        row = {"ticker": vals["ticker"], "cells": cells}
        if ccy is not None:
            row["ccy"] = ccy
        rows.append(row)
    return rows


@router.get("/", response_class=HTMLResponse)
def index(request: Request, state=Depends(get_state)):
    panels = [{"id": pid, "title": PANELS[pid][0], "columns": PANELS[pid][2],
               "ccy_filter": pid in CCY_FILTER_PANELS,
               "settle_filter": pid in SETTLE_FILTER_PANELS,  # selector CI / 24hs
               "hl_key": _HL_COL_KEY.get(pid),  # columna a resaltar (TIR; TNA en tasa fija)
               "chartable": bool(PANELS[pid][1]),  # tiene MD/TIR → muestra botón Gráfico
               "rows": _build_rows(pid, state)} for pid in PANEL_ORDER]
    return _TEMPLATES.TemplateResponse(
        request, "pages/index.html",
        {"panels": panels, "last_refresh": state.last_refresh,
         "default_layout": _read_default_layout()},
    )


@router.post("/panels/layout")
async def save_default_layout(request: Request):
    """Guarda el layout actual ({layout, hidden, cols}) como default del dashboard."""
    raw = await request.body()
    try:
        obj = json.loads(raw)  # debe ser JSON válido
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    try:
        os.makedirs(os.path.dirname(_LAYOUT_FILE), exist_ok=True)
        with open(_LAYOUT_FILE, "w", encoding="utf-8") as f:
            # Re-serializar (no el body crudo): normaliza y acota lo guardado. El
            # escapado anti-XSS se hace además al leer (_json_for_script).
            json.dump(obj, f, ensure_ascii=False)
    except OSError as e:
        logger.warning("No se pudo guardar el layout default: %s", e)
        return JSONResponse({"ok": False}, status_code=500)
    logger.info("Dashboard: layout default guardado.")
    return JSONResponse({"ok": True})


@router.delete("/panels/layout")
def clear_default_layout():
    """Borra el layout default (vuelve al auto-layout)."""
    try:
        os.remove(_LAYOUT_FILE)
    except OSError:
        pass
    return JSONResponse({"ok": True})


@router.get("/panels/{panel_id}/rows", response_class=HTMLResponse)
def panel_rows(panel_id: str, request: Request, settle: str = "24", state=Depends(get_state),
               provider=Depends(get_provider), rofex=Depends(get_rofex),
               fx=Depends(get_fx), indices=Depends(get_indices)):
    cols = PANELS.get(panel_id, (None, None, []))[2]
    if panel_id == "futuros":
        rows = _build_futuros_rows(rofex, fx, indices)
    elif settle.upper() == "CI" and panel_id in SETTLE_FILTER_PANELS:
        # Plazo CI (T+0): recalcular el panel on-demand con el snapshot CI del hub.
        metrics = _ci_metrics(panel_id, request, provider)
        rows = _build_rows(panel_id, state, provider, metrics_override=metrics or [])
    else:
        rows = _build_rows(panel_id, state, provider)
    return _TEMPLATES.TemplateResponse(
        request, "fragments/panel_rows.html",
        {"rows": rows, "ncols": len(cols)},
    )


# --- Gráfico (popup): dispersión TIR×MD + curva log por grupo --------------- #
def _chart_payload(panel_id: str, state, ccy_set: Optional[set]) -> List[dict]:
    """[{label, color, points:[{x:MD,y:TIR%,t:ticker}], curve:[{x,y}]}] por grupo.

    BONARES separa Bonares (BONAR) vs Globales (GLOBAL) en dos colores; el resto
    de los paneles de bonos van en una sola curva. Paneles sin MD/TIR → []."""
    if panel_id not in PANELS:
        return []
    title, types, _cols = PANELS[panel_id]
    if not types:  # valor_relativo / panel_lider / futuros / BEI: sin scatter MD/TIR
        return []
    buckets: dict = {}
    for m in state.metrics():
        inst = m.snapshot.instrument if m.snapshot else None
        if not inst or inst.instrument_type not in types:
            continue
        if not m.duration or m.duration <= 0 or m.tir is None:
            continue
        if ccy_set is not None and _ticker_ccy(inst.ticker) not in ccy_set:
            continue
        if panel_id == "bonares":
            label, color = ("Bonares", "#3a5fcf") if inst.instrument_type == "BONAR" \
                else ("Globales", "#d99a2b")
        else:
            label, color = title, "#3a5fcf"
        buckets.setdefault(label, {"color": color, "ms": []})["ms"].append(m)

    # En Tasa Fija el eje Y es TNA (base 365), no TIR/TEA — convertimos cada y.
    def _y(tea_dec: float) -> Optional[float]:
        if panel_id != "tasa_fija":
            return tea_dec * 100
        tna = FinancialEngine.tea_to_tna(tea_dec)
        return None if tna is None else tna * 100

    datasets: List[dict] = []
    for label, b in buckets.items():
        ms = b["ms"]
        points = []
        for m in ms:
            y = _y(m.tir)
            if y is None:
                continue
            points.append({"x": round(m.duration, 3), "y": round(y, 3),
                           "t": m.snapshot.instrument.ticker})
        curve: List[dict] = []
        fit = _fit_log_curve(ms)
        mds = sorted(m.duration for m in ms)
        if fit and len(mds) >= 3 and mds[-1] > mds[0]:
            a, bb = fit
            lo, hi = mds[0], mds[-1]
            for i in range(24):
                x = lo + (hi - lo) * i / 23.0
                if x > 0:
                    y = _y(a + bb * math.log(x))
                    if y is not None:
                        curve.append({"x": round(x, 3), "y": round(y, 3)})
        datasets.append({"label": label, "color": b["color"], "points": points, "curve": curve})
    return datasets


@router.get("/panels/{panel_id}/chart", response_class=HTMLResponse)
def panel_chart(panel_id: str, request: Request, ccy: str = "", state=Depends(get_state)):
    ccy_set = {c.strip().upper() for c in ccy.split(",") if c.strip()} or None
    datasets = _chart_payload(panel_id, state, ccy_set)
    title = PANELS.get(panel_id, (panel_id,))[0]
    y_label = "TNA" if panel_id == "tasa_fija" else "TIR"
    return _TEMPLATES.TemplateResponse(
        request, "fragments/panel_chart.html",
        {"title": title, "datasets_json": json.dumps(datasets), "y_label": y_label},
    )


# --- Compartir (popup p/ WhatsApp): columnas de la foto --- #
# El popup muestra el set de métricas CORE de _row_values en orden canónico, tomando las
# etiquetas/decimales del panel cuando la columna ya existe ahí (preserva 'TIR/TEA', 'DM',
# 'Var %', etc.). Se EXCLUYEN a propósito (decisión de David, 2026-06-05): 'short_name'
# (Emisor — redundante con el ticker salvo en ON, y muy ancho) y las ventanas de rendimiento
# Sem/1M/3M/YTD/1A (var_7d..var_1y — casi siempre vacías y comprimían la letra). Las
# columnas que igual queden 100% vacías para las especies del panel (ej. 'Categoría' en
# Tasa Fija) se descartan al vuelo en panel_share.
_ALL_BOND_COLS = [
    {"key": "ticker", "label": "Ticker", "kind": "text"},
    {"key": "category", "label": "Categoría", "kind": "text"},
    {"key": "vto", "label": "Vto", "kind": "date"},
    {"key": "dias", "label": "Días", "kind": "number", "decimals": 0},
    {"key": "days_next_coupon", "label": "Próx Cup", "kind": "number", "decimals": 0},
    {"key": "price", "label": "Precio", "kind": "number", "decimals": 2},
    {"key": "technical_value", "label": "V.Téc", "kind": "number", "decimals": 2},
    {"key": "parity", "label": "Paridad", "kind": "percent", "decimals": 2},
    {"key": "tir", "label": "TIR", "kind": "percent", "decimals": 2},
    {"key": "tna", "label": "TNA", "kind": "percent", "decimals": 2},
    {"key": "tem", "label": "TEM", "kind": "percent", "decimals": 2},
    {"key": "duration", "label": "MD", "kind": "number", "decimals": 2},
    {"key": "change_pct", "label": "Var%", "kind": "percent_signed", "decimals": 2},
    {"key": "volume", "label": "Vol $", "kind": "volume"},
]


# Columnas que la foto OMITE por panel:
#  · Soberanos hard-dollar (Bonares/Globales y Bopreales): se comparan por TIR (yield
#    en USD) → TNA(365)/TEM(365) (tasa en pesos), V.Téc y Días al vto no aplican
#    (decisión de David, 2026-06-08).
#  · CER: se compara por TIR real → TNA/TEM (nominales) y V.Téc agregan ruido; Próx Cup
#    tampoco aporta. Se conserva Días al vto (decisión de David, 2026-06-09).
_SHARE_DROP_COLS = {"bonares": {"tna", "tem", "technical_value", "dias"},
                    "bopreales": {"tna", "tem", "technical_value", "dias"},
                    "cer": {"tna", "tem", "technical_value", "days_next_coupon"}}


def _share_full_cols(full_cols: list, panel_id: str = "") -> list:
    """Columnas del popup = set canónico completo (_ALL_BOND_COLS), tomando label/
    decimales del panel cuando la columna ya existe ahí (preserva 'TIR/TEA', 'DM', etc.).
    En soberanos hard-dollar se omiten TNA/TEM/V.Téc/Días (no aplican: se comparan por TIR)."""
    by_key = {c["key"]: c for c in full_cols}
    drop = _SHARE_DROP_COLS.get(panel_id, set())
    out = []
    for base in _ALL_BOND_COLS:
        if base["key"] in drop:
            continue
        c = dict(base)
        ov = by_key.get(base["key"])
        if ov:
            if ov.get("label"):
                c["label"] = ov["label"]
            if "decimals" in ov:
                c["decimals"] = ov["decimals"]
        out.append(c)
    return out


def _drop_empty_share_cols(cols: list, rows: list) -> tuple:
    """Saca del popup las columnas (salvo la 1ª, el ticker) cuyas celdas están TODAS
    vacías ('—') para las especies mostradas — evita headers en blanco."""
    if not rows or len(cols) <= 1:
        return cols, rows
    drop = set()
    for i in range(1, len(cols)):
        if all(i >= len(r.get("cells", [])) or
               (r["cells"][i].get("text") in (None, "", "—")) for r in rows):
            drop.add(i)
    if not drop:
        return cols, rows
    cols = [c for i, c in enumerate(cols) if i not in drop]
    for r in rows:
        cs = r.get("cells")
        if cs:
            r["cells"] = [cell for i, cell in enumerate(cs) if i not in drop]
    return cols, rows

# Bajada (subtítulo) por panel para la foto — da contexto al cliente.
_PANEL_DESC = {
    "bonares": "Soberanos en dólares · ley local y NY",
    "bopreales": "Bopreales (BCRA) · hard-dollar",
    "cer": "Pesos ajustados por inflación (CER)",
    "tasa_fija": "Pesos a tasa fija",
    "tamar": "Tasa variable TAMAR / Dual",
    "dolar_linked": "Atados al dólar oficial",
    "obligaciones_negociables": "Obligaciones Negociables · deuda corporativa USD",
}

# Reparto del alto gráfico:tabla en la foto, por panel (lo usa fitShareRatio del template:
# alto_box = mult × alto_tabla). mult=0.5 → gráfico 1/3 · tabla 2/3 (default: curvas con
# muchas especies, ej. CER). mult=1.0 → 1/2 y 1/2 (TASA FIJA: pocas filas, así el gráfico
# no queda chico). Sin entrada → default 0.5.
_SHARE_CHART_MULT = {"tasa_fija": 1.0}
_SHARE_CHART_MULT_DEFAULT = 0.5


@router.get("/panels/{panel_id}/share", response_class=HTMLResponse)
def panel_share(panel_id: str, request: Request, ccy: str = "", state=Depends(get_state),
                provider=Depends(get_provider), rofex=Depends(get_rofex),
                fx=Depends(get_fx), indices=Depends(get_indices)):
    """Popup 'compartir' para WhatsApp: tabla + curva TIR×MD arriba (solo paneles
    chartables), en la MONEDA elegida en el panel (`ccy`, ej. MEP). Muestra todas las
    filas de esa moneda y —a diferencia del panel en pantalla— TODAS las columnas
    calculadas para los instrumentos (set canónico `_ALL_BOND_COLS`; se descartan las
    que quedan 100% vacías). Es read-only: no toca el dashboard (vive en #modal)."""
    # Futuros: popup propio con pestañas (Curva Rofex / Carry / Cobertura), no la
    # foto genérica (los futuros no tienen scatter TIR×MD). Liquidan T+0 (mismo día):
    # el day-count de TNA/TEA/crawl en _build_futuros_share cuenta días = vto − hoy
    # (no usa el settle T+1 BYMA). Sin subtítulo ni pie de fuente (decisión de diseño).
    if panel_id == "futuros":
        data = _build_futuros_share(rofex, fx, indices, state)
        resp = _TEMPLATES.TemplateResponse(
            request, "fragments/futuros_share.html",
            {"contracts_json": json.dumps(data.get("contracts", [])),
             "spot_txt": (f"{data['spot']:,.2f}" if data.get("spot") else "—"),
             "has_peso_curve": data.get("has_peso_curve", False)},
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    today = date.today()
    try:
        settle = settlement_byma_date(today, 1).strftime("%d/%m/%Y")
    except Exception:  # holiday data ausente → omitimos la liq.
        settle = ""
    title, _types, full_cols = PANELS.get(panel_id, (panel_id, set(), []))
    # El popup expone TODAS las columnas calculadas (no el subset curado del panel).
    # Bonos (types no vacío → usan _row_values): expandimos al set canónico completo y
    # reconstruimos las filas con esas columnas. Acciones/BEI/VR ya traen en sus filas
    # todo lo que calculan, así que se usan tal cual.
    if _types:
        cols = _share_full_cols(full_cols, panel_id)
        rows = _build_rows(panel_id, state, provider, cols_override=cols)
    else:
        cols = list(full_cols)
        rows = _build_rows(panel_id, state, provider)
    ccy_set = {c.strip().upper() for c in ccy.split(",") if c.strip()} or None
    # Moneda: SOLO aplica en paneles multi-moneda (CCY_FILTER_PANELS). En el resto se
    # ignora el ?ccy (no tienen columna de moneda → evita títulos falsos tipo "CER · MEP").
    # Si un panel multi-moneda llega sin moneda (deselect-all en el front), defaulteamos a
    # MEP para no mezclar ARS/MEP/CABLE (precios y curva incomparables) en la misma foto.
    if panel_id in CCY_FILTER_PANELS:
        if ccy_set is None:
            ccy_set = {"MEP"}
        ccy_label = " + ".join(sorted(ccy_set))
    else:
        ccy_set, ccy_label = None, ""
    # Filtra la tabla a la(s) moneda(s) elegida(s) (las filas sin 'ccy' quedan siempre).
    if ccy_set is not None:
        rows = [r for r in rows if r.get("ccy") is None or r["ccy"] in ccy_set]
    # Descarta columnas 100% vacías para estas especies (ej. Categoría en Tasa Fija,
    # ventanas de rendimiento sin histórico) — nunca la 1ª (ticker).
    cols, rows = _drop_empty_share_cols(cols, rows)
    datasets = _chart_payload(panel_id, state, ccy_set)
    y_label = "TNA" if panel_id == "tasa_fija" else "TIR"
    # Subtítulo de la foto: bajada del panel + fecha y liquidación (T+1 BYMA, ya arriba).
    resp = _TEMPLATES.TemplateResponse(
        request, "fragments/panel_share.html",
        {"title": title, "columns": cols, "rows": rows,
         "hl_key": _HL_COL_KEY.get(panel_id),
         "chart_mult": _SHARE_CHART_MULT.get(panel_id, _SHARE_CHART_MULT_DEFAULT),
         "desc": _PANEL_DESC.get(panel_id, ""), "ccy_label": ccy_label,
         "asof": today.strftime("%d/%m/%Y"), "settle": settle,
         "badge": ("%s vs Duración" % y_label),
         "datasets_json": json.dumps(datasets), "has_chart": bool(datasets),
         "y_label": y_label},
    )
    # No cachear: el popup/export se actualiza seguido; evita que el navegador sirva
    # un fragmento viejo (cacheado) tras un cambio de template.
    resp.headers["Cache-Control"] = "no-store"
    return resp
