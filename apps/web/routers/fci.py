"""Panel FCI (CAFCI) — HTMX.

GET /fci          → página: filtros (tipo renta + moneda + búsqueda + período + métrica).
GET /fci/rows     → fragmento tabla filtrada/ordenada.

Reusa CAFCIProvider (catálogo + matriz de rendimientos diaria, disk-mirror).
Datos diarios (no live) → no vive del refresh de 5s.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from apps.web.deps import get_cafci

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

_PERIOD_LABELS = {"dias_7": "7d", "mes_1": "1m", "dias_90": "3m",
                  "dias_180": "6m", "ytd": "YTD", "meses_12": "12m"}
_PERIODS = list(_PERIOD_LABELS)


def _options(cafci) -> tuple:
    funds = cafci.list_funds(limit=None)
    tipos = sorted({f["tipo_renta"] for f in funds if f.get("tipo_renta")})
    monedas = sorted({f["moneda"] for f in funds if f.get("moneda")})
    return tipos, monedas


def _rows_ctx(cafci, tipo, moneda, q, sort, metric) -> dict:
    if sort not in _PERIODS:
        sort = "mes_1"
    if metric not in ("tna", "directo"):
        metric = "tna"
    funds = cafci.list_funds(tipo_renta=tipo or None, moneda=moneda or None, query=q or None,
                             sort=sort, direction="desc", metric=metric, limit=150)
    return {"funds": funds, "periods": _PERIODS, "labels": _PERIOD_LABELS,
            "sort": sort, "metric": metric}


@router.get("/fci", response_class=HTMLResponse)
def fci_page(request: Request, cafci=Depends(get_cafci)):
    tipos, monedas = _options(cafci)
    ctx = {"tipos": tipos, "monedas": monedas, "meta": cafci.get_meta()}
    ctx.update(_rows_ctx(cafci, "", "", "", "mes_1", "tna"))
    return _TEMPLATES.TemplateResponse(request, "pages/fci.html", ctx)


@router.get("/fci/rows", response_class=HTMLResponse)
def fci_rows(request: Request, tipo: str = "", moneda: str = "", q: str = "",
             sort: str = "mes_1", metric: str = "tna", cafci=Depends(get_cafci)):
    return _TEMPLATES.TemplateResponse(request, "fragments/fci_rows.html",
                                       _rows_ctx(cafci, tipo, moneda, q, sort, metric))
