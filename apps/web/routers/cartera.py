"""Router de la Cartera (HTMX).

GET    /cartera                 → página (resumen + posiciones + alta + flujos).
POST   /cartera/holding         → alta/edición de tenencia → re-render del cuerpo.
DELETE /cartera/holding/{t}     → baja → re-render del cuerpo.

Reusa cartera_store (data/cartera.json) + portfolio.build_portfolio con los
precios/TIR/MD vivos de AppState (motor Fase 1) + portfolio_cashflows con los
Instruments del CatalogRepository.
"""

from __future__ import annotations

import html
import logging
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from apps.web import cartera_store
from apps.web.deps import get_fx, get_repo, get_state
from apps.web.templates import TEMPLATES as _TEMPLATES
from core.domain import portfolio
from core.domain.instrument_groups import (
    BOPREALES, CER, DOLAR_LINKED, DUAL_TAMAR, SOBERANOS, TAMAR, TASA_FIJA,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_GRUPO = {}
for _label, _types in (("Soberano", SOBERANOS), ("Bopreal", BOPREALES), ("Tasa Fija", TASA_FIJA),
                       ("CER", CER), ("Dolar Linked", DOLAR_LINKED),
                       ("TAMAR", TAMAR + DUAL_TAMAR)):
    for _t in _types:
        _GRUPO[_t] = _label


def _metrics_by_ticker(state) -> dict:
    out = {}
    for m in state.metrics():
        inst = m.snapshot.instrument if m.snapshot else None
        if not inst:
            continue
        out[inst.ticker] = {
            "price": m.snapshot.price,
            "tir": (m.tir * 100) if m.tir is not None else None,  # % units (convención server)
            "md": m.duration,
            "grupo": _GRUPO.get(inst.instrument_type, "—"),
            "spread_curva": None,
            "currency": portfolio.position_currency(inst.instrument_type, inst.ticker),
            "short_name": inst.short_name,
        }
    return out


def _quote(fx, method: str) -> Optional[float]:
    """Lee una punta del provider de FX de forma tolerante (None si falla)."""
    if not fx:
        return None
    try:
        fn = getattr(fx, method, None)
        v = fn() if callable(fn) else None
    except Exception:
        return None
    return v if (v and v > 0) else None


def _fx_rates(fx) -> Tuple[Optional[float], Optional[float]]:
    """(MEP, CCL) de venta para valuar las posiciones en dólares.

    Una pata …D liquida al **MEP** (dólar bolsa) y una …C al **CCL** — NO al
    mayorista/A3500, que es el oficial y tiene brecha contra ambos (antes se usaba
    el mayorista para TODO el libro y el sesgo se propagaba a pesos, TIR/MD
    ponderadas y P&L de escenarios, porque todo se pondera por `market_value_ars`).

    Degradación: si el provider no expone una punta, se cae a la otra y por último
    al mayorista — una valuación aproximada es preferible a dejar en blanco todas
    las posiciones en dólares.
    """
    mep = _quote(fx, "get_mep_venta")
    ccl = _quote(fx, "get_ccl_venta")
    if mep is None or ccl is None:
        fallback = mep or ccl or _quote(fx, "get_mayorista_venta")
        mep = mep or fallback
        ccl = ccl or fallback
    return mep, ccl


def _context(state, repo, fx) -> dict:
    """Contexto compartido por la página y el fragmento (misma valuación en ambos)."""
    holdings = cartera_store.list_holdings()
    mep, ccl = _fx_rates(fx)
    pf = portfolio.build_portfolio(holdings, _metrics_by_ticker(state),
                                   fx_usd_ars=mep, fx_cable_ars=ccl)
    by_ticker = {i.ticker: i for i in repo.get_all_instruments()}
    return {"pf": pf, "cashflows": portfolio.portfolio_cashflows(holdings, by_ticker)}


# Banner de error del alta. El fragmento `cartera_body.html` no tiene slot propio,
# así que se antepone al swap de `#cartera-body` (mismo patrón visual que `.abm-err`).
_ERR_BANNER = (
    '<div class="cartera-err" role="alert" style="margin:0 12px 8px;padding:8px 10px;'
    'border:1px solid var(--neg);border-radius:6px;background:rgba(168,36,43,.1);'
    'color:var(--neg);font-weight:600">⚠ No se guardó: {msg}</div>'
)


def _render_body(request: Request, state, repo, fx, error: Optional[str] = None) -> HTMLResponse:
    resp = _TEMPLATES.TemplateResponse(
        request, "fragments/cartera_body.html", _context(state, repo, fx),
    )
    if not error:
        return resp
    banner = _ERR_BANNER.format(msg=html.escape(str(error)))
    return HTMLResponse(banner + resp.body.decode("utf-8"), status_code=resp.status_code)


@router.get("/cartera", response_class=HTMLResponse)
def cartera_page(request: Request, state=Depends(get_state), repo=Depends(get_repo), fx=Depends(get_fx)):
    return _TEMPLATES.TemplateResponse(request, "pages/cartera.html",
                                       _context(state, repo, fx))


@router.post("/cartera/holding", response_class=HTMLResponse)
def add_holding(request: Request, ticker: str = Form(...), nominal: float = Form(...),
                cost_price: Optional[float] = Form(None), note: str = Form(""),
                state=Depends(get_state), repo=Depends(get_repo), fx=Depends(get_fx)):
    error = None
    try:
        cartera_store.upsert_holding(ticker, nominal, cost_price, note)
    except ValueError as e:
        # NUNCA tragar el error (mismo criterio que el ABM): el form se auto-resetea
        # con `hx-on::after-request`, así que sin aviso el usuario lee un alta que
        # nunca se persistió como si hubiera salido bien.
        logger.warning("Cartera: alta rechazada (%s): %s", ticker, e)
        error = str(e)
    return _render_body(request, state, repo, fx, error=error)


@router.delete("/cartera/holding/{ticker}", response_class=HTMLResponse)
def remove_holding(ticker: str, request: Request,
                   state=Depends(get_state), repo=Depends(get_repo), fx=Depends(get_fx)):
    cartera_store.delete_holding(ticker)
    return _render_body(request, state, repo, fx)
