"""Catálogo de Productos BYMA — HTMX SSR.

Unifica el universo de cotizantes (reusa el buscador de /abm/universe + ficha
técnica rica) con las familias live nuevas de la API open: cauciones (curva de
tasas), índices MERVAL/BURCAP (chart) y SENEBI-ON (universo ON bilateral).

GET /catalogo                → página (tiles de familias + universo + secciones).
GET /catalogo/indices        → fragmento: cards MERVAL/BURCAP + sparkline.
GET /catalogo/cauciones      → fragmento: tabla de cauciones por plazo.
GET /catalogo/senebi         → fragmento: tabla SENEBI-ON (filtro client-side).
GET /catalogo/ficha?ticker=  → fragmento: ficha técnica rica (ley/amort/montos).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from apps.web.templates import TEMPLATES as _TEMPLATES
from core.infrastructure.byma import catalog_products as cp
from core.infrastructure.byma.universe import categories, count

router = APIRouter()


def _spark(points, w: float = 120.0, h: float = 28.0) -> str:
    """Polyline `x,y …` normalizada a un viewbox w×h para el mini-gráfico SVG."""
    pts = [p for p in (points or []) if p]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1.0
    n = len(pts)
    return " ".join(f"{i / (n - 1) * w:.1f},{h - (v - lo) / rng * h:.1f}"
                    for i, v in enumerate(pts))


@router.get("/catalogo", response_class=HTMLResponse)
def catalogo_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "pages/catalogo.html", {
        "byma_cats": categories(),
        "byma_count": count(),
    })


@router.get("/catalogo/indices", response_class=HTMLResponse)
def catalogo_indices(request: Request):
    # TTL corto: cada miss agrega 1 punto al sparkline intradía acumulado.
    snap = cp._cached("indices", 45.0, cp.index_snapshot)
    cards = [{**ix, "spark": _spark(ix.get("points"))} for ix in (snap or [])]
    return _TEMPLATES.TemplateResponse(request, "fragments/catalogo_indices.html",
                                       {"cards": cards})


@router.get("/catalogo/cauciones", response_class=HTMLResponse)
def catalogo_cauciones(request: Request):
    rows = cp.cauciones_cached()
    return _TEMPLATES.TemplateResponse(request, "fragments/catalogo_cauciones.html",
                                       {"rows": rows})


@router.get("/catalogo/senebi", response_class=HTMLResponse)
def catalogo_senebi(request: Request, q: str = Query("")):
    rows = cp.senebi_on_cached()
    q = (q or "").strip().upper()
    if q:
        rows = [r for r in rows if q in r["symbol"].upper()]
    return _TEMPLATES.TemplateResponse(request, "fragments/catalogo_senebi.html",
                                       {"rows": rows[:500], "total": len(rows), "q": q})


@router.get("/catalogo/ficha", response_class=HTMLResponse)
def catalogo_ficha(request: Request, ticker: str = Query("")):
    ticker = (ticker or "").strip().upper()
    ficha = cp.ficha_for(ticker) if ticker else {}
    return _TEMPLATES.TemplateResponse(request, "fragments/catalogo_ficha.html",
                                       {"ticker": ticker, "ficha": ficha})
