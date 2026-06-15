"""Router del Monitor BCRA (HTMX, read-only).

GET /bcra → reservas / A3500 / TAMAR / CER (último valor + historia reciente).
Reusa BCRAIndicesProvider (app.state.indices), hidratado de disco + top-up BCRA.
"""

from __future__ import annotations

from datetime import date
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from apps.web.deps import get_indices
from apps.web.templates import TEMPLATES as _TEMPLATES

router = APIRouter()


@router.get("/bcra", response_class=HTMLResponse)
def bcra_page(request: Request, indices=Depends(get_indices)):
    today = date.today()
    ctx = {
        "reservas": indices.get_reservas_brutas(),
        "reservas_delta": indices.get_reservas_delta(),
        "a3500": indices.get_a3500(),
        "tamar": indices.get_tamar(),
        "cer": indices.get_cer(today),
        "reservas_hist": list(reversed(indices.get_reservas_history(30))),
        "tamar_hist": list(reversed(indices.get_tamar_history(30))),
        "a3500_hist": list(reversed(indices.get_a3500_history(30))),
    }
    return _TEMPLATES.TemplateResponse(request, "pages/bcra.html", ctx)
