"""Página O.N's (Obligaciones Negociables) — app cliente + endpoint de datos.

GET  /on       → página (carga apps/web/static/js/on.js, que hace fetch a /on/data).
GET  /on/data  → dataset JSON (mismo shape que el mock `window.ON_DATA`) armado en
                 `apps/web/on_service.py` desde el snapshot vivo (AppState), con el
                 sector clasificado al vuelo. Memoizado por revisión/día; GZip global.
GET  /on/pdf   → PDF del universo completo (Hard Dollar por defecto) — lo usa el CLI.
POST /on/pdf   → PDF de una lista EXPLÍCITA de tickers: el botón 🖨️ manda los que el
                 sidebar de facetas dejó visibles, así el documento coincide con la
                 pantalla (`fpdf2` faltante o sin fuentes TTF → 503, no 500).
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from apps.web.deps import get_fx, get_state
from apps.web.on_service import get_on_dataset
from apps.web.templates import TEMPLATES as _TEMPLATES

router = APIRouter()

# Tope defensivo: el universo ON son ~100 patas; un body enorme sólo puede ser abuso.
_MAX_TICKERS = 2000


class OnPdfRequest(BaseModel):
    tickers: Optional[List[str]] = Field(default=None, max_length=_MAX_TICKERS)
    charts: bool = True
    dl: bool = False


@router.get("/on", response_class=HTMLResponse)
def on_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "pages/on.html", {})


@router.get("/on/data")
def on_data(state=Depends(get_state), fx=Depends(get_fx)):
    return JSONResponse(get_on_dataset(state, fx=fx))


def _pdf_response(state, fx, *, include_dl: bool, charts: bool,
                  tickers: Optional[List[str]] = None) -> Response:
    """Arma el PDF y lo devuelve como descarga. `fpdf2` (dep opcional) ausente o sin
    fuente TrueType disponible → 503 con mensaje accionable, no 500 con traceback."""
    try:
        from apps.web.on_pdf import build_on_pdf
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail="El export a PDF necesita fpdf2: instalá requirements.lock.") from e

    data = get_on_dataset(state, fx=fx)
    try:
        blob, _ = build_on_pdf(data, include_dl=include_dl, charts=charts, tickers=tickers)
    except FileNotFoundError as e:                      # sin fuentes TTF en el host
        raise HTTPException(status_code=503, detail=str(e)) from e
    fname = f"Obligaciones_Negociables_{data.get('today') or 'AR'}.pdf"
    return Response(content=blob, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/on/pdf")
def on_pdf(state=Depends(get_state), fx=Depends(get_fx), dl: bool = False, charts: bool = True):
    """PDF del universo completo — mismo armado que `scripts/export_on_pdf.py` (resumen
    por sector + detalle Sector›Emisor›Título). `dl=true` suma Dollar Linked;
    `charts=false` lo arma sin los scatter (más rápido)."""
    return _pdf_response(state, fx, include_dl=dl, charts=charts)


@router.post("/on/pdf")
def on_pdf_filtered(req: OnPdfRequest, state=Depends(get_state), fx=Depends(get_fx)):
    """PDF de los tickers que manda el cliente (los visibles tras aplicar las facetas).
    Sin `tickers` se comporta igual que el GET."""
    return _pdf_response(state, fx, include_dl=req.dl, charts=req.charts, tickers=req.tickers)
