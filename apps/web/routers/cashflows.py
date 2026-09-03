"""Router de Cashflows (HTMX, read-only).

GET /cashflows → calendario de flujos futuros de TODOS los instrumentos del
catálogo (próximos `days` días), ordenado por fecha. Reusa CatalogRepository.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from apps.web.deps import get_repo
from apps.web.templates import TEMPLATES as _TEMPLATES
from core.domain.portfolio import position_currency

router = APIRouter()


@router.get("/cashflows", response_class=HTMLResponse)
def cashflows_page(request: Request,
                   # Acotado: `today + timedelta(days=days)` tira OverflowError apenas
                   # se pasa de date.max (≈2.912.000 días) y nadie lo atrapa → 500 con
                   # traceback. Fuera de rango, FastAPI devuelve un 422 limpio.
                   days: int = Query(180, ge=1, le=3650),
                   repo=Depends(get_repo)):
    today = date.today()
    horizon = today + timedelta(days=days)
    events = []
    for inst in repo.get_all_instruments():
        ccy = position_currency(inst.instrument_type, inst.ticker)
        for cf in inst.get_future_cashflows(today):
            if cf.date > horizon:
                continue
            if not (cf.amortization or cf.interest):
                continue
            concepto = ("Renta + Amort." if (cf.amortization and cf.interest)
                        else ("Amortización" if cf.amortization else "Renta"))
            events.append({
                "date": cf.date, "ticker": inst.ticker, "concepto": concepto,
                "amort": cf.amortization, "interest": cf.interest,
                "total": cf.amortization + cf.interest, "ccy": ccy,
            })
    events.sort(key=lambda e: (e["date"], e["ticker"]))
    return _TEMPLATES.TemplateResponse(request, "pages/cashflows.html",
                                       {"events": events, "days": days})
