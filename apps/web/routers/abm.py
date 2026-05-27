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

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from apps.web import instruments_abm as abm_store
from apps.web.deps import get_repo

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/abm", response_class=HTMLResponse)
def abm_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "pages/abm.html", {
        "instruments": abm_store.list_instruments(),
        "sheets": list(abm_store.SHEET_SCHEMAS.keys()),
    })


@router.get("/abm/form", response_class=HTMLResponse)
def abm_form(request: Request, sheet: str, ticker: str = ""):
    if sheet not in abm_store.SHEET_SCHEMAS:
        return HTMLResponse("<div class='err'>Hoja desconocida</div>", status_code=400)
    values = {}
    if ticker:
        inst = abm_store.get_instrument(ticker)
        if inst:
            values = inst.get("fields", {})
    return _TEMPLATES.TemplateResponse(request, "fragments/abm_form.html", {
        "sheet": sheet,
        "fields": abm_store.SHEET_SCHEMAS[sheet]["fields"],
        "label": abm_store.SHEET_SCHEMAS[sheet]["label"],
        "values": values,
        "ticker": ticker,
    })


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
