"""Panel FCI (CAFCI) — app cliente + endpoint de datos.

GET /fci        → página (carga apps/web/static/js/fci.js, que hace fetch a /fci/data).
GET /fci/data   → dataset JSON `{meta, funds}` (armado en `apps/web/fci_service.py`:
                  CAFCI enriquecido + AUM ArgentinaDatos + lente A3500/CER + flujos
                  reales de fci_history). Memoizado por corte/día; servido con GZip.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from apps.web.deps import get_cafci, get_fx, get_indices
from apps.web.fci_service import get_fci_dataset

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/fci", response_class=HTMLResponse)
def fci_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "pages/fci.html", {})


@router.get("/fci/data")
def fci_data(cafci=Depends(get_cafci), indices=Depends(get_indices), fx=Depends(get_fx)):
    return JSONResponse(get_fci_dataset(cafci, indices, fx))
