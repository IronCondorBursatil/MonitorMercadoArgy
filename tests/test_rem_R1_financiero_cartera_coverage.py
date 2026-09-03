"""Auditoría R1 — el P&L parcial de la cartera se declara en la UI.

El hallazgo #3 hizo que `pnl_ars`/`pnl_pct` se calculen SÓLO sobre las posiciones
con las dos puntas conocidas (costo Y precio vivo) — correcto — y expuso
`summary.cost_coverage` para decir qué fracción del libro cubre ese número. Pero
ningún template lo consumía: el usuario veía un P&L que ya no reconcilia con las
cards "Valor mercado" / `total_cost_ars` y no tenía ninguna señal de por qué.

Acá se fija que `fragments/cartera_body.html` lo muestre cuando la cobertura es
parcial (y que no ensucie la card cuando cubre todo el libro).
"""
from __future__ import annotations

from apps.web.templates import TEMPLATES as _TEMPLATES

_TPL = "fragments/cartera_body.html"


def _render(cost_coverage, *, pnl=1234.0):
    summary = {
        "as_of": "2026-09-03",
        "total_market_value_ars": 1_000_000.0,
        "total_cost_ars": 900_000.0,
        "pnl_ars": pnl, "pnl_pct": 0.05,
        "cost_coverage": cost_coverage,
        "weighted_tir": 12.0, "portfolio_md": 3.0, "weighted_spread_curva": None,
        "by_grupo": {}, "by_currency": {}, "n_positions": 3,
        "fx_usd_ars": 1400.0, "fx_cable_ars": 1500.0,
    }
    return _TEMPLATES.get_template(_TPL).render(
        pf={"positions": [], "summary": summary}, cashflows={"months": []})


def test_muestra_la_cobertura_cuando_el_pnl_es_parcial():
    html = _render(0.62)
    assert "62" in html
    assert "cobertura" in html.lower() or "cubre" in html.lower()


def test_no_ensucia_la_card_cuando_cubre_todo_el_libro():
    html = _render(1.0)
    assert "cobertura" not in html.lower() and "cubre" not in html.lower()


def test_avisa_cuando_no_hay_ninguna_punta_de_costo():
    """`cost_coverage is None` = ninguna posición tiene costo cargado → el P&L
    viene en `—`; el aviso tiene que explicar por qué."""
    html = _render(None, pnl=None)
    assert "cobertura" in html.lower() or "cubre" in html.lower() or "sin costo" in html.lower()


def test_la_cobertura_parcial_es_visible_en_la_card_de_pnl():
    """No alcanza con ponerlo en un `title=`: tiene que verse como texto."""
    html = _render(0.62)
    import re
    # el texto del aviso vive fuera de cualquier atributo HTML
    visible = re.sub(r"<[^>]*>", " ", html)
    assert "62" in visible
