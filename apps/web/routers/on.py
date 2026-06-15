"""Página O.N's (Obligaciones Negociables) — app cliente + endpoint de datos.

GET /on       → página (carga apps/web/static/js/on.js, que hace fetch a /on/data).
GET /on/data  → dataset JSON (mismo shape que el mock `window.ON_DATA`) armado en
                `apps/web/on_service.py` desde el snapshot vivo (AppState), con el
                sector clasificado al vuelo. Memoizado por revisión/día; GZip global.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from apps.web.deps import get_fx, get_state
from apps.web.on_service import get_on_dataset
from apps.web.templates import TEMPLATES as _TEMPLATES

router = APIRouter()


@router.get("/on", response_class=HTMLResponse)
def on_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "pages/on.html", {})


@router.get("/on/data")
def on_data(state=Depends(get_state), fx=Depends(get_fx)):
    return JSONResponse(get_on_dataset(state, fx=fx))
