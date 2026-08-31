"""Refresco solo de lo que se ve: los hx-trigger periódicos llevan el filtro
`[mrRefreshOK(this)]` (definido en base.html) para no pedir datos cuando la pestaña
del navegador está en segundo plano o el panel está cerrado con la X.

Verificado en navegador real (Playwright, 2026-08-31): con `document.hidden=true`
los 13 paneles hacen 0 requests en 15s (antes ~27); al volver refrescan al instante
vía `tabvisible`; un panel con display:none hace 0 mientras el resto sigue.

Estos tests son la red de regresión del CONTRATO (que el filtro siga cableado en el
markup): un `hx-trigger` sin filtro vuelve a martillar el server en silencio.
"""

import re

from fastapi.testclient import TestClient

from apps.web.app import app

# Se inspeccionan SOLO los atributos hx-trigger reales — no la prosa de la página
# (los comentarios JS de base.html nombran `sse:refresh`/`every Ns` y darían falso
# positivo si se buscara sobre el HTML entero).
_TRIGGER_ATTR = re.compile(r'hx-trigger="([^"]*)"')
# Triggers periódicos que SIEMPRE deben ir filtrados (los que se repiten solos).
_PERIODIC = re.compile(r"(sse:refresh|every\s+\d+s)(?!\[mrRefreshOK)")


def _unfiltered_periodic(html: str) -> list:
    """Triggers periódicos sin el filtro, dentro de atributos hx-trigger."""
    out = []
    for attr in _TRIGGER_ATTR.findall(html):
        out.extend(m[0] if isinstance(m, tuple) else m for m in _PERIODIC.findall(attr))
    return out


def _html(path: str) -> str:
    with TestClient(app) as c:
        r = c.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    return r.text


def test_helper_mrrefreshok_esta_definido():
    """El filtro vive en base.html → presente en TODAS las páginas."""
    html = _html("/")
    assert "window.mrRefreshOK" in html
    assert "visibilitychange" in html
    assert 'htmx.trigger(document.body, "tabvisible")' in html


def test_paneles_del_dashboard_filtran_sus_refrescos():
    html = _html("/")
    sobrantes = _unfiltered_periodic(html)
    assert not sobrantes, f"triggers periódicos SIN filtro en /: {sobrantes}"


def test_dashboard_conserva_load_y_sse():
    """El filtro no debe haber roto el refresco real: `load` sin filtrar (si no, un
    panel podría no cargar nunca) + sse:refresh filtrado + tabvisible para re-sincronizar."""
    html = _html("/")
    assert "load, sse:refresh[mrRefreshOK(this)]" in html
    assert "tabvisible[mrRefreshOK(this)] from:body" in html


def test_header_global_filtra_sus_refrescos():
    """El badge de salud (15s) y el strip de cotizaciones (60s) están en base.html:
    corren en todas las páginas, así que son los que más se acumulan."""
    html = _html("/")
    assert "every 15s[mrRefreshOK(this)]" in html      # /health/badge
    assert "every 60s[mrRefreshOK(this)]" in html      # /header/cards


def test_catalogo_filtra_sus_refrescos():
    html = _html("/catalogo")
    sobrantes = _unfiltered_periodic(html)
    assert not sobrantes, f"triggers periódicos SIN filtro en /catalogo: {sobrantes}"


def test_curva_no_pollea_en_segundo_plano():
    """La curva usa setInterval propio (no htmx) → guard explícito de document.hidden."""
    html = _html("/curva")
    assert "if (!document.hidden) loadCurva();" in html
