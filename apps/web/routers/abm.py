"""ABM de instrumentos (HTMX) — backend SQLite transaccional (§5.5).

GET    /abm                     → lista + selector de hoja + form.
GET    /abm/form                → form schema-driven de una hoja (prefill si ticker).
POST   /abm/save                → alta/edición (SQLAlchemy txn) + refresh del cache.
DELETE /abm/instrument/{ticker} → baja + refresh.

Escribe el catálogo SQLite vía `apps.web.instruments_abm` (transaccional) y
refresca el cache en memoria del `CatalogRepository` desde SQLite (sin re-leer
el Excel: el master ya es solo semilla).
"""

from __future__ import annotations

from pathlib import Path

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from apps.web import instruments_abm as abm_store
from apps.web.bond_detail import calculate
from apps.web.deps import (
    get_fx, get_hub, get_indices, get_provider, get_repo, get_state,
)
from core.domain.instrument_groups import PANEL_LIDER

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/abm", response_class=HTMLResponse)
def abm_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "pages/abm.html", {
        "instruments": abm_store.list_instruments(),
        "sheets": list(abm_store.SHEET_SCHEMAS.keys()),
    })


@router.get("/abm/data912", response_class=HTMLResponse)
def abm_data912(request: Request, hub=Depends(get_hub), repo=Depends(get_repo)):
    """Sidebar del ABM: tickers que cotizan en Data912 pero no están en el catálogo,
    agrupados por endpoint de origen. Se computa en vivo del snapshot del hub →
    al dar de alta una especie, el operador tiene a mano lo que falta cargar."""
    snapshot = hub.snapshot() if hub else {}
    sources = hub.sources() if hub else {}
    catalog = {i.ticker for i in repo.get_all_instruments()}
    groups = abm_store.unknown_data912_tickers(snapshot, sources, catalog, exclude=PANEL_LIDER)
    corp_subgroups = abm_store.group_corp_tickers(groups.get("corp", [])) if "corp" in groups else None
    return _TEMPLATES.TemplateResponse(request, "fragments/abm_data912.html", {
        "groups": groups,
        "corp_subgroups": corp_subgroups,
    })


def _live_metrics(state, values: dict, key: str):
    """Métricas vivas (TIR/MD/V.Téc/paridad/precio) del bono.
    Prioridad: MEP (D) → CABLE (C) → ARS (O) → key como fallback."""
    candidates = [
        values.get("ticker_mep"),
        values.get("ticker_ccl"),
        values.get("ticker_ars"),
        key,
    ]
    for t in candidates:
        if not t:
            continue
        m = state.by_ticker(t)
        if m is not None:
            return {
                "ticker": t,
                "price": m.snapshot.price if m.snapshot else None,
                "tir": m.tir, "md": m.duration,
                "vtec": m.technical_value, "par": m.parity,
            }
    return None


@router.get("/abm/form", response_class=HTMLResponse)
def abm_form(request: Request, sheet: str, key: str = "", state=Depends(get_state)):
    if sheet not in abm_store.SHEET_SCHEMAS:
        return HTMLResponse("<div class='err'>Hoja desconocida</div>", status_code=400)
    values, cashflows, metrics = {}, [], None
    if key:
        # `key` es cualquier ticker del bono → prefill con sus 3 slots de moneda.
        inst = abm_store.get_instrument(key)
        if inst:
            values = inst.get("fields", {})
            cashflows = inst.get("cashflows", [])
        metrics = _live_metrics(state, values, key)
    return _TEMPLATES.TemplateResponse(request, "fragments/abm_form.html", {
        "sheet": sheet,
        "fields": abm_store.SHEET_SCHEMAS[sheet]["fields"],
        "label": abm_store.SHEET_SCHEMAS[sheet]["label"],
        "values": values,
        "ticker": key,
        "cashflows": cashflows,
        "metrics": metrics,
    })


@router.post("/abm/calc", response_class=HTMLResponse)
def abm_calc(request: Request, ticker: str = Form(...),
             price: Optional[float] = Form(None), tir_pct: Optional[float] = Form(None),
             repo=Depends(get_repo), provider=Depends(get_provider),
             indices=Depends(get_indices), fx=Depends(get_fx)):
    """Recalcula precio↔TIR de un ticker concreto del bono (mismo motor que el modal
    de detalle). El selector de moneda elige la pata: USD (D/C) o pesos."""
    if tir_pct is not None and price is None:
        res = calculate(ticker, repo, provider, indices, fx, mode="from_tir",
                        tir=tir_pct / 100.0, settlement_lag=1)
    else:
        res = calculate(ticker, repo, provider, indices, fx, mode="from_price",
                        price=price, price_mode="dirty", settlement_lag=1)
    return _TEMPLATES.TemplateResponse(request, "fragments/calc_result.html", {"res": res})


def _list_response(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "fragments/abm_list.html",
                                       {"instruments": abm_store.list_instruments()})


@router.post("/abm/save", response_class=HTMLResponse)
async def abm_save(request: Request, sheet: str = Form(...), repo=Depends(get_repo)):
    form = await request.form()
    fields = {k: v for k, v in form.items() if k != "sheet"}
    try:
        abm_store.save_instrument(sheet, fields)  # cashflows=None → preserva/synth
        repo.reload(reseed_from_excel=False)       # refresca el cache desde SQLite
    except (ValueError, KeyError):
        pass
    return _list_response(request)


@router.delete("/abm/instrument/{ticker}", response_class=HTMLResponse)
def abm_delete(ticker: str, request: Request, repo=Depends(get_repo)):
    abm_store.delete_instrument(ticker)
    repo.reload(reseed_from_excel=False)
    return _list_response(request)
