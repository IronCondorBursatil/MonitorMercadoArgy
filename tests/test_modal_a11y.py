"""E5 — helper a11y de modal/drawer (focus-trap + restaurar foco + Esc) + cierre en
error. Smoke Python-side (sin runtime JS): valida presencia y cableado del helper en
base.html, index.html y el bundle on.js. El comportamiento DOM se verifica a mano."""

from fastapi.testclient import TestClient

from apps.web.app import app


def test_base_defines_a11y_modal_helper():
    """base.html define window.A11yModal con focus-trap (Tab) + Esc en su <script>
    inline (alcanzable por todas las páginas que extienden base.html)."""
    with TestClient(app) as c:
        html = c.get("/").text
    assert "window.A11yModal" in html
    # piezas del focus-trap / Esc en el mismo documento
    assert 'e.key === "Escape"' in html
    assert 'e.key !== "Tab"' in html
    assert "querySelectorAll(SEL)" in html


def test_cer_drawer_wired_to_helper():
    """El drawer CER (cerOpen/cerClose) usa el helper para atrapar foco y restaurarlo."""
    with TestClient(app) as c:
        html = c.get("/").text
    assert "A11yModal.open(d, {" in html
    assert "A11yModal.close(d)" in html


def test_modal_closes_on_fetch_error():
    """Un fetch fallido con hx-target=#modal (detalle/chart/share) NO deja el modal
    colgado vacío: se pinta un estado de error con botón cerrar."""
    with TestClient(app) as c:
        html = c.get("/").text
    assert 'addEventListener("htmx:responseError"' in html
    assert 'addEventListener("htmx:sendError"' in html
    assert "No se pudo cargar el contenido" in html


def test_on_chart_modal_wired_to_helper():
    """El modal del gráfico TIR-vs-MD de /on (openUniModal/closeUniModal del bundle)
    engancha el helper; el rebuild de build_on_static.py se corrió."""
    with TestClient(app) as c:
        js = c.get("/static/js/on.js").text
    assert "A11yModal.open(" in js and "A11yModal.close(" in js
    assert 'id="uni-chart-backdrop"' in c.get("/on").text
