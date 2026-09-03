"""Escenarios / Stress (HTMX).

GET  /escenarios        → página con sliders Δtasa (bps) + ΔFX (%).
POST /escenarios/run    → reprice: ΔP por bono (mercado) + P&L de la cartera.

Reusa core.domain.scenarios (reprice analítico MD+convexidad + betas FX) sobre
los InstrumentMetrics de AppState; la P&L de cartera usa portfolio.build_portfolio
sobre las tenencias de cartera_store.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from apps.web import cartera_store
from apps.web.deps import get_fx, get_state
from apps.web.routers.cartera import _fx_rates, _GRUPO, _metrics_by_ticker
from apps.web.templates import TEMPLATES as _TEMPLATES
from core.domain import portfolio, scenarios
from core.domain.portfolio import position_currency

router = APIRouter()


def _market_rows(state, d_tir_bps: float, d_fx_pct: float) -> list:
    rows = []
    for m in state.metrics():
        inst = m.snapshot.instrument if m.snapshot else None
        if not inst or m.duration is None:
            continue
        ccy = position_currency(inst.instrument_type, inst.ticker)
        sh = scenarios.shock_position(
            price=m.snapshot.price, md=m.duration, currency=ccy,
            grupo=_GRUPO.get(inst.instrument_type, "—"),
            d_tir_bps=d_tir_bps, d_fx_pct=d_fx_pct,
        )
        rows.append({
            "ticker": inst.ticker, "grupo": _GRUPO.get(inst.instrument_type, "—"),
            "md": m.duration, "dp_pct": sh["dp_pct"], "dp_ars_pct": sh["dp_ars_pct"],
        })
    rows.sort(key=lambda r: abs(r["dp_ars_pct"] or 0.0), reverse=True)
    return rows


def _cartera_pnl(state, fx, d_tir_bps: float, d_fx_pct: float) -> dict:
    """P&L del libro ante el escenario, valuado EXACTAMENTE como /cartera.

    Usa el mismo helper `cartera._fx_rates` (MEP para las …D, CCL para las …C) en
    vez del mayorista/A3500: con el oficial, el MISMO libro valía distinto en
    /cartera y en /escenarios (brecha típica del 5% al 30%), y como
    `portfolio_shock` pondera cada ΔP por `market_value_ars`, el sesgo se
    propagaba al P&L agregado y al ranking de contribuciones.
    """
    holdings = cartera_store.list_holdings()
    mep, ccl = _fx_rates(fx)
    pf = portfolio.build_portfolio(holdings, _metrics_by_ticker(state),
                                   fx_usd_ars=mep, fx_cable_ars=ccl)
    return scenarios.portfolio_shock(pf["positions"], d_tir_bps=d_tir_bps, d_fx_pct=d_fx_pct)


def _ctx(state, fx, d_tir_bps: float, d_fx_pct: float) -> dict:
    return {
        "rows": _market_rows(state, d_tir_bps, d_fx_pct),
        "pnl": _cartera_pnl(state, fx, d_tir_bps, d_fx_pct),
        "d_tir_bps": d_tir_bps, "d_fx_pct": d_fx_pct,
    }


@router.get("/escenarios", response_class=HTMLResponse)
def escenarios_page(request: Request, state=Depends(get_state), fx=Depends(get_fx)):
    return _TEMPLATES.TemplateResponse(request, "pages/escenarios.html",
                                       _ctx(state, fx, 0.0, 0.0))


@router.post("/escenarios/run", response_class=HTMLResponse)
def escenarios_run(request: Request, d_tir_bps: float = Form(0.0), d_fx_pct: float = Form(0.0),
                   state=Depends(get_state), fx=Depends(get_fx)):
    return _TEMPLATES.TemplateResponse(request, "fragments/escenario_result.html",
                                       _ctx(state, fx, d_tir_bps, d_fx_pct))
