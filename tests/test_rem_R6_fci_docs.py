"""Remediación lote R6 (FCI) — la fórmula del flujo que se documenta y que ve el usuario
tiene que ser la que el código calcula.

El fix del hallazgo 1 cambió la valuación de las cuotapartes suscriptas/rescatadas de
`Δccp × vcp` a `Δccp × precio de cuotaparte` (= `patrimonio/ccp`, con fallback `vcp/1000`
porque ArgentinaDatos publica el VCP **por cada 1.000 cuotapartes**). La diferencia es de
1.000×, así que la fórmula vieja quedó no solo desactualizada sino ERRÓNEA por tres
órdenes de magnitud — y seguía escrita en el docstring de `net_flows`, en el docstring del
dominio y en cuatro textos del panel (vista Flujos, tab Flujos del detalle y el pie de
fuentes), que son los que lee el usuario para interpretar el número.
"""

from pathlib import Path

from core.infrastructure.fci_history import FCIHistoryStore, net_flow_series

_ROOT = Path(__file__).resolve().parent.parent
_FCI_JS = _ROOT / "apps" / "web" / "static" / "js" / "fci.js"

# La fórmula vieja, en las variantes que quedaron escritas.
_STALE = ("Δccp × vcp", "Δccp x vcp", "Δccp×VCP", "Δccp × VCP",
          "Δcuotapartes × VCP", "Δcuotapartes×VCP")


def _stale_in(text):
    return [p for p in _STALE if p in text]


def test_los_docstrings_del_store_no_prometen_la_formula_vieja():
    for doc in (FCIHistoryStore.net_flows.__doc__, net_flow_series.__doc__):
        assert doc and not _stale_in(doc), doc
    assert "precio de cuotaparte" in FCIHistoryStore.net_flows.__doc__


def test_el_dominio_fci_documenta_la_formula_vigente():
    for mod in ("__init__.py", "dataset.py"):
        text = (_ROOT / "core" / "domain" / "fci" / mod).read_text(encoding="utf-8")
        assert not _stale_in(text), (mod, _stale_in(text))
        assert "precio de cuotaparte" in text


def test_el_panel_no_le_muestra_al_usuario_la_formula_vieja():
    """4 textos del cliente: 'acumulando' de la vista Flujos, la nota del gráfico
    agregado, la del tab Flujos del detalle y el pie de fuentes."""
    text = _FCI_JS.read_text(encoding="utf-8")
    assert not _stale_in(text), _stale_in(text)
    assert text.count("precio de cuotaparte") >= 4
