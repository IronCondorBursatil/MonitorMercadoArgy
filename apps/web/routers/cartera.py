"""Router de la Cartera (HTMX).

GET    /cartera                 → página (resumen + posiciones + alta + flujos).
POST   /cartera/holding         → alta/edición de tenencia → re-render del cuerpo.
DELETE /cartera/holding/{t}     → baja → re-render del cuerpo.

Reusa cartera_store (data/cartera.json) + portfolio.build_portfolio con los
precios/TIR/MD vivos de AppState (motor Fase 1) + portfolio_cashflows con los
Instruments del CatalogRepository.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from apps.web import cartera_store
from apps.web.deps import get_fx, get_repo, get_state
from apps.web.templates import TEMPLATES as _TEMPLATES
from core.domain import portfolio
from core.domain.instrument_groups import (
    BOPREALES, CER, DOLAR_LINKED, DUAL_TAMAR, SOBERANOS, TAMAR, TASA_FIJA,
)

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


def _render_body(request: Request, state, repo, fx) -> HTMLResponse:
    holdings = cartera_store.list_holdings()
    fx_rate = None
    try:
        fx_rate = fx.get_mayorista_venta() if fx else None
    except Exception:
        fx_rate = None
    pf = portfolio.build_portfolio(holdings, _metrics_by_ticker(state), fx_usd_ars=fx_rate)
    by_ticker = {i.ticker: i for i in repo.get_all_instruments()}
    cfs = portfolio.portfolio_cashflows(holdings, by_ticker)
    return _TEMPLATES.TemplateResponse(
        request, "fragments/cartera_body.html",
        {"pf": pf, "cashflows": cfs},
    )


@router.get("/cartera", response_class=HTMLResponse)
def cartera_page(request: Request, state=Depends(get_state), repo=Depends(get_repo), fx=Depends(get_fx)):
    holdings = cartera_store.list_holdings()
    fx_rate = None
    try:
        fx_rate = fx.get_mayorista_venta() if fx else None
    except Exception:
        fx_rate = None
    pf = portfolio.build_portfolio(holdings, _metrics_by_ticker(state), fx_usd_ars=fx_rate)
    by_ticker = {i.ticker: i for i in repo.get_all_instruments()}
    cfs = portfolio.portfolio_cashflows(holdings, by_ticker)
    return _TEMPLATES.TemplateResponse(request, "pages/cartera.html",
                                       {"pf": pf, "cashflows": cfs})


@router.post("/cartera/holding", response_class=HTMLResponse)
def add_holding(request: Request, ticker: str = Form(...), nominal: float = Form(...),
                cost_price: Optional[float] = Form(None), note: str = Form(""),
                state=Depends(get_state), repo=Depends(get_repo), fx=Depends(get_fx)):
    try:
        cartera_store.upsert_holding(ticker, nominal, cost_price, note)
    except ValueError:
        pass
    return _render_body(request, state, repo, fx)


@router.delete("/cartera/holding/{ticker}", response_class=HTMLResponse)
def remove_holding(ticker: str, request: Request,
                   state=Depends(get_state), repo=Depends(get_repo), fx=Depends(get_fx)):
    cartera_store.delete_holding(ticker)
    return _render_body(request, state, repo, fx)
