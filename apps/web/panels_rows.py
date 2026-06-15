"""Row-building logic for bond panels (extracted from panels.py).

Contains all pure computation helpers (_fmt, _cell_class, _row_values, etc.)
and the _build_*_rows / _chart_payload / _share_* functions.
The FastAPI route handlers remain in routers/panels.py.

External callers:
  on_service.py  → _ley_of, _ticker_ccy
  routers/curva.py → _fit_log_curve
"""

from __future__ import annotations

import math
import logging
from datetime import date
from typing import List, Optional

import numpy as np

from core.domain.currency import ccy_from_suffix
from core.domain.instrument_groups import PANEL_LIDER
from core.domain.on_classification import sector_for, sector_meta
from core.domain.portfolio import position_currency
from core.domain.services import FinancialEngine
from core.infrastructure.futures_provider import (
    DEFAULT_SYMBOLS as ROFEX_SYMBOLS,
    implied_tna,
    parse_contract_maturity,
    resolve_spot_for_tna,
)
from apps.web.routers.panels_schema import (
    PANELS, CCY_FILTER_PANELS, LEY_FILTER_PANELS,
    PRICE_REQUIRED_PANELS,
    _HL_COL_KEY, _BEI_TABLE_KEY, _VR_COLS, _PANEL_LIDER_COLS, _FUTUROS_COLS,
)

logger = logging.getLogger(__name__)


# ── Helpers: moneda y ley ────────────────────────────────────────────────────

def _ley_of(inst) -> str:
    """Ley aplicable de un instrumento para el filtro del panel ON: AR (Argentina)
    / EXT (extranjera; sin dato → EXT, misma convención que el pricing MEP/CCL)."""
    return "AR" if inst.is_ley_argentina else "EXT"


def _ticker_ccy(ticker: str) -> str:
    """Moneda de una especie soberana por su sufijo: D=MEP, C=CABLE, resto=ARS."""
    return ccy_from_suffix(ticker)


# ── Rich/cheap: ajuste de curva log ─────────────────────────────────────────

# Grupos para el ajuste de curva log (TIR = a + b·ln(MD)) — un fit por grupo.
# Sólo curvas peso de flavor único (Tasa Fija nominal, CER real): los soberanos
# hard-dollar conviven en 3 flavors (peso/MEP/cable) con escalas de precio/TIR
# distintas, y mezclarlos da rich/cheap sin sentido.
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


# ── Helpers: formato de celdas ───────────────────────────────────────────────

def _next_coupon_date(inst, today: date) -> Optional[date]:
    return min((cf.date for cf in (inst.cashflows or [])
                if cf.date > today and cf.interest > 0), default=None)


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


# ── Builders de filas ────────────────────────────────────────────────────────

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


# Futuros: popup tabbed (Curva Rofex / Carry / Cobertura)
_TASA_FIJA_TYPES = {"LECAP", "BONCAP", "BONOFIJA"}


def _implied_rates(fx_ref: Optional[float], spot: Optional[float],
                   days: Optional[int]) -> tuple:
    """(tna_lineal, tea, crawl_mensual) en decimales desde futuro/spot/días.

    - TNA lineal (base 365):   (F/S − 1) · 365/d
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
        fx_ref = last if last is not None else settle
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
    if cols_override is not None:
        cols = cols_override
    today = date.today()
    ccy_filterable = panel_id in CCY_FILTER_PANELS
    ley_filterable = panel_id in LEY_FILTER_PANELS
    price_required = panel_id in PRICE_REQUIRED_PANELS
    if metrics_override is not None:
        metrics = [m for m in metrics_override
                   if m.snapshot and m.snapshot.instrument]
    else:
        metrics = [m for m in state.metrics()
                   if m.snapshot and m.snapshot.instrument and m.snapshot.instrument.instrument_type in types]
    metrics.sort(key=lambda m: (m.duration is None, m.duration or 0.0))
    rows = []
    for m in metrics:
        if price_required and not m.snapshot.price:
            continue
        vals = _row_values(m, today)
        if panel_id == "obligaciones_negociables":
            vals["sector"] = sector_meta(sector_for(m.snapshot.instrument)).short
        ccy = _ticker_ccy(vals["ticker"]) if ccy_filterable else None
        cells = []
        hl_key = _HL_COL_KEY.get(panel_id)
        for c in cols:
            raw = vals.get(c["key"])
            dec = c.get("decimals", 2)
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
        if ley_filterable:
            row["ley"] = _ley_of(m.snapshot.instrument)
        rows.append(row)
    return rows


# ── Gráfico (popup): dispersión TIR×MD + curva log ───────────────────────────

def _chart_payload(panel_id: str, state, ccy_set: Optional[set],
                   ley_set: Optional[set] = None) -> List[dict]:
    """[{label, color, points:[{x:MD,y:TIR%,t:ticker}], curve:[{x,y}]}] por grupo.

    BONARES separa Bonares (BONAR) vs Globales (GLOBAL) en dos colores; el resto
    de los paneles de bonos van en una sola curva. Paneles sin MD/TIR → [].
    `ley_set` ({AR,EXT}) espeja el filtro de ley del panel ON."""
    if panel_id not in PANELS:
        return []
    title, types, _cols = PANELS[panel_id]
    if not types:
        return []
    if panel_id not in LEY_FILTER_PANELS:
        ley_set = None
    buckets: dict = {}
    for m in state.metrics():
        inst = m.snapshot.instrument if m.snapshot else None
        if not inst or inst.instrument_type not in types:
            continue
        if not m.duration or m.duration <= 0 or m.tir is None:
            continue
        if ccy_set is not None and _ticker_ccy(inst.ticker) not in ccy_set:
            continue
        if ley_set is not None and _ley_of(inst) not in ley_set:
            continue
        if panel_id == "bonares":
            label, color = ("Bonares", "#3a5fcf") if inst.instrument_type == "BONAR" \
                else ("Globales", "#d99a2b")
        else:
            label, color = title, "#3a5fcf"
        buckets.setdefault(label, {"color": color, "ms": []})["ms"].append(m)

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


# ── Share popup: columnas canónicas ─────────────────────────────────────────

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

_SHARE_DROP_COLS = {"bonares": {"tna", "tem", "technical_value", "dias"},
                    "bopreales": {"tna", "tem", "technical_value", "dias"},
                    "cer": {"tna", "tem", "technical_value", "days_next_coupon"}}


def _share_full_cols(full_cols: list, panel_id: str = "") -> list:
    """Columnas del popup = set canónico completo (_ALL_BOND_COLS), tomando label/
    decimales del panel cuando la columna ya existe ahí."""
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
