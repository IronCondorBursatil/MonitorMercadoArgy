"""Router del tab de Opciones AR — HTMX SSR.

GET /options                       → página completa (BBG grid + builder + scanner)
GET /options/chain?u=&v=           → fragmento: tabla de la chain (calls ← K → puts)
GET /options/smile?u=&v=           → fragmento: IV smile (canvas + JSON embebido)
GET /options/oi?u=&v=              → fragmento: OI heatmap por strike
GET /options/scanner?...           → fragmento: tabla scanner con tasas implícitas
GET /options/sidebar               → fragmento: watchlist de subyacentes

Toda la data viene de `state.options()` (lista de OptionItem que el refresh loop
ya enriqueció). El strategy builder es 100% client-side: lee la chain embebida
y calcula payoff/griegos en el browser (mismo modelo que en los mocks).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from apps.web.json_script import json_for_script
from apps.web.deps import get_state
from core.domain.options.analytics import run_analytics
from core.domain.options.chain import months_for, underlyings_summary
from core.domain.options.strategies import PRESET_NAMES

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


# ── Helpers ─────────────────────────────────────────────────────────────────

def _select_default_underlying(items: list, prefer: str = "GGAL") -> Optional[str]:
    """Underlying inicial: preferimos GGAL (siempre líquido); fallback al de mayor volumen."""
    summary = underlyings_summary(items)
    if not summary:
        return None
    by_u = {s["underlying"]: s for s in summary}
    return prefer if prefer in by_u else summary[0]["underlying"]


def _chain_data(items: list, underlying: str, month: str) -> dict:
    """Estructura común para chain.html (filas por strike con call+put)."""
    items_u = [it for it in items if it.underlying == underlying and it.month_code == month]
    strikes = sorted({it.strike for it in items_u})
    by_key = {(it.kind, it.strike): it for it in items_u}
    rows = []
    spot = items_u[0].spot if items_u else 0.0
    for K in strikes:
        c = by_key.get(("C", K))
        p = by_key.get(("V", K))
        rows.append({"strike": K, "call": c.to_dict() if c else None,
                     "put": p.to_dict() if p else None,
                     "is_atm": False})
    # Marcar ATM (primer strike >= spot).
    atm_idx = next((i for i, r in enumerate(rows) if r["strike"] >= spot), -1)
    if 0 <= atm_idx < len(rows):
        rows[atm_idx]["is_atm"] = True
    return {"rows": rows, "spot": spot, "n": len(items_u),
            "chain_json": json_for_script([it.to_dict() for it in items_u])}


# ── Página principal ────────────────────────────────────────────────────────

@router.get("/options", response_class=HTMLResponse)
def options_page(request: Request, state=Depends(get_state)):
    items = state.options()
    underlying = _select_default_underlying(items) or "GGAL"
    months = months_for(items, underlying)
    month = months[0] if months else ""
    return _TEMPLATES.TemplateResponse(
        request, "pages/options.html",
        {"underlying": underlying, "month": month, "months": months,
         "has_data": bool(items), "presets": PRESET_NAMES},
    )


# ── Fragments ───────────────────────────────────────────────────────────────

@router.get("/options/sidebar", response_class=HTMLResponse)
def options_sidebar(request: Request, u: str = "", state=Depends(get_state)):
    items = state.options()
    summary = underlyings_summary(items)
    return _TEMPLATES.TemplateResponse(
        request, "fragments/options_sidebar.html",
        {"summary": summary, "current": u or (summary[0]["underlying"] if summary else "")},
    )


@router.get("/options/chain", response_class=HTMLResponse)
def options_chain(request: Request, u: str = Query(""), v: str = Query(""),
                  state=Depends(get_state)):
    items = state.options()
    if not u:
        u = _select_default_underlying(items) or ""
    months = months_for(items, u)
    if not v or v not in months:
        v = months[0] if months else ""
    data = _chain_data(items, u, v) if (u and v) else {"rows": [], "spot": 0.0, "n": 0,
                                                       "chain_json": "[]"}
    return _TEMPLATES.TemplateResponse(
        request, "fragments/options_chain.html",
        {"underlying": u, "month": v, "months": months, **data},
    )


@router.get("/options/smile", response_class=HTMLResponse)
def options_smile(request: Request, u: str = Query(""), v: str = Query(""),
                  state=Depends(get_state)):
    items = [it for it in state.options()
             if it.underlying == u and it.month_code == v and it.iv is not None]
    pts_call = [{"x": it.strike, "y": it.iv * 100} for it in items if it.kind == "C"]
    pts_put = [{"x": it.strike, "y": it.iv * 100} for it in items if it.kind == "V"]
    return _TEMPLATES.TemplateResponse(
        request, "fragments/options_smile.html",
        {"underlying": u, "month": v,
         "pts_call_json": json_for_script(pts_call),
         "pts_put_json": json_for_script(pts_put)},
    )


@router.get("/options/oi", response_class=HTMLResponse)
def options_oi(request: Request, u: str = Query(""), v: str = Query(""),
               state=Depends(get_state)):
    items = [it for it in state.options() if it.underlying == u and it.month_code == v]
    agg: dict[float, float] = {}
    for it in items:
        agg[it.strike] = agg.get(it.strike, 0.0) + (it.open_interest or 0.0)
    entries = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
    entries = [(K, oi) for K, oi in entries if oi > 0][:14]
    max_oi = entries[0][1] if entries else 1.0
    return _TEMPLATES.TemplateResponse(
        request, "fragments/options_oi.html",
        {"underlying": u, "month": v, "entries": entries, "max_oi": max_oi},
    )


@router.get("/options/scanner", response_class=HTMLResponse)
def options_scanner(request: Request,
                    kind: str = Query(""), liq: str = Query(""),
                    vto: str = Query(""), q: str = Query(""),
                    sort_by: str = Query("tna_bruta"),
                    sort_dir: str = Query("desc"),
                    state=Depends(get_state)):
    items = state.options()
    rows = []
    for it in items:
        if kind and it.kind != kind:
            continue
        if vto and it.month_code != vto:
            continue
        if liq == "bid" and it.bid <= 0:
            continue
        if liq == "vol" and (it.volume or 0) < 100:
            continue
        q_lc = q.lower()
        if q and q_lc not in it.ticker.lower() and q_lc not in it.underlying.lower():
            continue
        rows.append(it.to_dict())

    def _key(r):
        v = r.get(sort_by)
        return (v is None, v if v is not None else 0)
    rows.sort(key=_key, reverse=(sort_dir == "desc"))

    months = sorted({it.month_code for it in items})
    return _TEMPLATES.TemplateResponse(
        request, "fragments/options_scanner.html",
        {"rows": rows[:300], "total": len(rows), "truncated": max(0, len(rows) - 300),
         "kind": kind, "liq": liq, "vto": vto, "q": q,
         "sort_by": sort_by, "sort_dir": sort_dir, "months": months},
    )


# ── Analytics (POST JSON) ───────────────────────────────────────────────────

class _LegInput(BaseModel):
    ticker: str
    qty: int


class _AnalyticsBody(BaseModel):
    legs: list[_LegInput]


_EMPTY_ANALYTICS = {"prob_profit": None, "ev_profit": None, "ev_loss": None,
                    "profit_ranges": [], "strategy_cost": None}


@router.post("/options/analytics")
def options_analytics(payload: _AnalyticsBody, state=Depends(get_state)) -> JSONResponse:
    """Calcula P(profit), EV+ y EV− de la estrategia armada por el usuario.

    El client envía solo `{ticker, qty}` por leg; aquí resolvemos contra
    `state.options()` para extraer kind/strike/precios/IV/expiry y delegamos
    a `core.domain.options.analytics.run_analytics`.
    """
    items = state.options()
    if not items or not payload.legs:
        return JSONResponse(_EMPTY_ANALYTICS)

    by_tk = {it.ticker: it for it in items}
    legs = [l.model_dump() for l in payload.legs if l.qty != 0]
    # Primera leg válida define el contexto (underlying, vto, spot).
    ref = next((by_tk[l["ticker"].upper()] for l in legs
                if l["ticker"].upper() in by_tk), None)
    if not ref:
        return JSONResponse(_EMPTY_ANALYTICS)

    # IV ATM del mismo subyac+vto (proxy razonable como vol del random walk).
    chain = [it for it in items
             if it.underlying == ref.underlying and it.month_code == ref.month_code
             and it.iv is not None]
    atm = min(chain, key=lambda x: abs(x.strike - ref.spot)) if chain else None
    atm_iv = atm.iv if atm else None

    result = run_analytics(
        legs=legs, items_by_ticker=by_tk, spot=ref.spot,
        atm_iv=atm_iv, r=0.40,
        start_date=date.today(), target_date=ref.expiry,
    )
    return JSONResponse(result or _EMPTY_ANALYTICS)
