"""Router del popup de detalle de bono (HTMX).

GET  /bond/{ticker}/detail   → fragmento modal (Detalles + cashflows + calculadora).
POST /bond/{ticker}/metrics  → recalcula métricas desde precio o TIR (calculadora).

Reusa apps.web.bond_detail.get_bond_detail/calculate (ya sólidos) con los
providers de app.state. Reemplaza los tabs DETALLES/CALCULADORA del SPA.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from apps.web.bond_detail import calculate, get_bond_detail
from apps.web.deps import get_fx, get_indices, get_provider, get_repo

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/bond/{ticker}/detail", response_class=HTMLResponse)
def detail(ticker: str, request: Request, lag: int = 1,
           repo=Depends(get_repo), provider=Depends(get_provider),
           indices=Depends(get_indices), fx=Depends(get_fx)):
    d = get_bond_detail(ticker, repo, provider, indices, fx, settlement_lag=lag)
    if d is None:
        return HTMLResponse(
            f'<div class="modal-overlay" onclick="if(event.target===this)this.remove()">'
            f'<div class="modal-card"><div class="modal-head"><b>{ticker}</b>'
            f'<button class="x" onclick="document.getElementById(\'modal\').innerHTML=\'\'">✕</button>'
            f'</div><div class="modal-body err">Instrumento no encontrado</div></div></div>',
            status_code=404,
        )
    return _TEMPLATES.TemplateResponse(request, "fragments/bond_detail.html", {"d": d, "lag": lag})


@router.post("/bond/{ticker}/metrics", response_class=HTMLResponse)
def metrics(ticker: str, request: Request,
            settlement_lag: int = Form(1),
            price: Optional[float] = Form(None),
            tir_pct: Optional[float] = Form(None),
            repo=Depends(get_repo), provider=Depends(get_provider),
            indices=Depends(get_indices), fx=Depends(get_fx)):
    if tir_pct is not None and price is None:
        res = calculate(ticker, repo, provider, indices, fx, mode="from_tir",
                        tir=tir_pct / 100.0, settlement_lag=settlement_lag)
    else:
        res = calculate(ticker, repo, provider, indices, fx, mode="from_price",
                        price=price, price_mode="dirty", settlement_lag=settlement_lag)
    return _TEMPLATES.TemplateResponse(request, "fragments/calc_result.html", {"res": res})
