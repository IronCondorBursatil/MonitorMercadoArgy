"""Router del popup de detalle de bono (HTMX).

GET  /bond/{ticker}/detail   → fragmento modal (Detalles + cashflows + calculadora).
POST /bond/{ticker}/metrics  → recalcula métricas desde precio o TIR (calculadora).

Reusa apps.web.bond_detail.get_bond_detail/calculate (ya sólidos) con los
providers de app.state. Reemplaza los tabs DETALLES/CALCULADORA del SPA.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from apps.web.bond_detail import calculate, cer_projection, get_bond_detail
from apps.web.deps import get_fx, get_indices, get_provider, get_repo, get_state
from apps.web.templates import TEMPLATES as _TEMPLATES
from core.infrastructure.rem_provider import REMProvider

router = APIRouter()


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


def _to_float(raw):
    """Parse tolerante (acepta coma decimal). None si no es número."""
    if raw is None:
        return None
    try:
        return float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _rem_provider():
    return REMProvider()


def _sendero(state):
    """Filas del sendero mensual BEI/REM del último cómputo BEI (o None)."""
    bei = state.bei_tables() if state is not None else None
    return (bei or {}).get("sendero") if bei else None


def _render_cer_drawer(request, data, *, ticker, lag, price, mode, unif, raw_inputs):
    unif_val = unif
    if unif_val is None and data.get("default_unif") is not None:
        unif_val = round(data["default_unif"] * 100, 2)
    return _TEMPLATES.TemplateResponse(request, "fragments/cer_drawer_body.html", {
        "d": data, "ticker": ticker, "lag": lag, "price": price,
        "mode": mode, "unif": unif_val, "raw_inputs": raw_inputs,
    })


@router.get("/bond/{ticker}/cer", response_class=HTMLResponse)
def cer_drawer(ticker: str, request: Request, lag: int = 1,
               price: Optional[float] = None,
               repo=Depends(get_repo), provider=Depends(get_provider),
               indices=Depends(get_indices), fx=Depends(get_fx), state=Depends(get_state)):
    """Cuerpo del cajón 'Proyección CER' (carga inicial): escenarios + valor CER
    por mes hasta el vto, con REM/BEI de referencia."""
    if price is None and state is not None:
        price = state.price_of(ticker)
    data = cer_projection(ticker, repo, provider, indices, fx,
                          price_dirty=price, settlement_lag=lag,
                          bei_sendero=_sendero(state), rem_provider=_rem_provider())
    return _render_cer_drawer(request, data, ticker=ticker, lag=lag, price=price,
                              mode="uniforme", unif=None, raw_inputs={})


@router.post("/bond/{ticker}/cer", response_class=HTMLResponse)
async def cer_drawer_calc(ticker: str, request: Request,
                          repo=Depends(get_repo), provider=Depends(get_provider),
                          indices=Depends(get_indices), fx=Depends(get_fx),
                          state=Depends(get_state)):
    """Recalcula el cajón con el escenario del usuario (uniforme o por mes)."""
    form = await request.form()
    lag = int(_to_float(form.get("lag")) or 1)
    mode = form.get("mode", "uniforme")
    price = _to_float(form.get("price"))

    custom_infl = custom_monthly = None
    raw_inputs: dict = {}
    if mode == "custom":
        custom_monthly = {}
        for key, val in form.items():
            if not key.startswith("infl_"):
                continue
            ym = key[5:]
            raw_inputs[ym] = val
            dec = _to_float(val)
            if dec is None:
                continue
            try:
                y, m = ym.split("-")
                custom_monthly[(int(y), int(m))] = dec / 100.0
            except ValueError:
                pass
        custom_monthly = custom_monthly or None
    else:
        ci = _to_float(form.get("unif"))
        custom_infl = (ci / 100.0) if ci is not None else None

    # to_thread: `cer_projection` hace red SÍNCRONA (REM + BCRA, con timeouts de 10 s
    # cada uno). Corriendo en la corrutina congelaba el event loop entero —medido:
    # 8,5 s de lag, y hasta 40-60 s si los providers timeoutean— lo que frena TODAS
    # las conexiones SSE y el resto de los paneles de todos los clientes.
    data = await asyncio.to_thread(
        cer_projection, ticker, repo, provider, indices, fx,
        price_dirty=price, settlement_lag=lag,
        bei_sendero=_sendero(state), rem_provider=_rem_provider(),
        custom_infl_monthly=custom_infl, custom_monthly=custom_monthly)
    return _render_cer_drawer(request, data, ticker=ticker, lag=lag, price=price,
                              mode=mode, unif=form.get("unif"), raw_inputs=raw_inputs)
