"""Router de Cashflows (HTMX, read-only).

GET /cashflows → calendario de flujos futuros de TODOS los instrumentos del
catálogo (próximos `days` días), ordenado por fecha. Reusa CatalogRepository.

Los tipos de PAYOFF ANALÍTICO (TAMAR PURO / DUAL / DUAL_CER_TAMAR) no tienen schedule
—su pago sale de una fórmula cerrada— y por eso eran INVISIBLES acá. Su evento de
vencimiento se sintetiza desde `inst.maturity_date`: la fila ancla de la DB no sirve
para esto (`_orm_to_domain` la filtra a propósito, y de todos modos abajo se descartan
los montos en cero), así que la visibilidad se resuelve en esta capa, con los montos
en em-dash — porque el importe NO se conoce hasta el vencimiento.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from apps.web.deps import get_repo
from apps.web.templates import TEMPLATES as _TEMPLATES
from core.domain.instrument_groups import has_closed_form_payoff
from core.domain.portfolio import position_currency

# Concepto de la fila sintetizada. Dice explícitamente por qué no hay importes: sin esto
# una fila con tres em-dash se lee como un dato faltante, no como una convención.
_ANALITICO = "Vencimiento · payoff analítico (TAMAR)"

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
        if has_closed_form_payoff(inst.instrument_type):
            vto = inst.maturity_date
            if vto and today < vto <= horizon:
                events.append({
                    "date": vto, "ticker": inst.ticker, "concepto": _ANALITICO,
                    "amort": None, "interest": None, "total": None, "ccy": ccy,
                })
            continue
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
