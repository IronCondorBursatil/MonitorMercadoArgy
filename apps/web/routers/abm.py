"""ABM de instrumentos (HTMX).

GET    /abm                     → lista + selector de hoja + form.
GET    /abm/form                → form schema-driven de una hoja (prefill si ticker).
POST   /abm/save                → alta/edición (reusa instruments_abm) + reseed SQLite.
DELETE /abm/instrument/{ticker} → baja + reseed.

Reusa `apps.web.instruments_abm` (escritura Excel atómica, ya testeada) y
`CatalogRepository.reload()` para re-sembrar SQLite desde el Excel editado — el
cutover transaccional a SQLAlchemy (§5.5) queda para después; esto da la
funcionalidad de edición reutilizando código probado.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from apps.web import instruments_abm as abm_store
from apps.web.deps import get_repo
from config.settings import settings

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
_XLSX = str(settings.master_xlsx)


@router.get("/abm", response_class=HTMLResponse)
def abm_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "pages/abm.html", {
        "instruments": abm_store.list_instruments(_XLSX),
        "sheets": list(abm_store.SHEET_SCHEMAS.keys()),
    })


@router.get("/abm/form", response_class=HTMLResponse)
def abm_form(request: Request, sheet: str, ticker: str = ""):
    if sheet not in abm_store.SHEET_SCHEMAS:
        return HTMLResponse("<div class='err'>Hoja desconocida</div>", status_code=400)
    values = {}
    if ticker:
        inst = abm_store.get_instrument(_XLSX, ticker)
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
                                       {"instruments": abm_store.list_instruments(_XLSX)})


@router.post("/abm/save", response_class=HTMLResponse)
async def abm_save(request: Request, sheet: str = Form(...), repo=Depends(get_repo)):
    form = await request.form()
    fields = {k: v for k, v in form.items() if k != "sheet"}
    try:
        abm_store.save_instrument(_XLSX, sheet, fields)  # cashflows=None → preserva/synth
        repo.reload()  # re-siembra SQLite desde el Excel editado
    except (ValueError, KeyError):
        pass
    return _list_response(request)


@router.delete("/abm/instrument/{ticker}", response_class=HTMLResponse)
def abm_delete(ticker: str, request: Request, repo=Depends(get_repo)):
    abm_store.delete_instrument(_XLSX, ticker)
    repo.reload()
    return _list_response(request)
