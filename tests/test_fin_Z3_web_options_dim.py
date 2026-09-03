"""Cierre Z3 (ítem 7) — el aviso "sin precio utilizable" del chain de opciones era
INVISIBLE.

`fragments/options_chain.html::leg_cell` marca tres estados por celda de punta:

  · precio de entrada real  → `clickable`, arma la pata;
  · entra a mid/last        → `clickable` + `⚠` y el `title` avisa que NO entra a la
                              punta que se está viendo;
  · **sin ningún precio utilizable** (punta, mid y último en 0) → la pata entraría al
    nominal de 0,01, así que la celda va SIN `onclick`, con `class="… dim"` y el motivo
    en el `title`.

El tercer caso era el único que no se veía: `static/css/options.css` no tenía ninguna
regla para `td.dim`, así que la celda se pintaba **igual que una clickeable** (el verde
de `td.C` / el rojo de `td.V`, con el hover de fila encima). La única señal de que no se
podía operar era que el click no hacía nada — y el `title` sólo aparece si el usuario
deja el mouse quieto encima. Un aviso que no se ve es un aviso que no existe.

Los tests no chequean valores de diseño (color exacto, opacidad): chequean que la celda
sin precio **resuelva distinto** que una operable del mismo lado, que es la propiedad que
importa. Para eso resuelven la cascada del CSS con un matcher mínimo sobre los selectores
del archivo (todos simples: tag + clases + descendencia).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.noauth

RAIZ = Path(__file__).resolve().parent.parent
CSS = RAIZ / "apps" / "web" / "static" / "css" / "options.css"
FRAGMENTO = "fragments/options_chain.html"

# Propiedades por las que un aviso se VE. `cursor` queda afuera a propósito: sólo cambia
# el puntero, no el píxel — no alcanza como único distintivo.
_VISUALES = ("color", "opacity", "font-style", "text-decoration", "background",
             "background-color", "filter", "font-weight")

_REGLA = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)
_COMPOUND = re.compile(r"^([a-zA-Z]*)((?:\.[-\w]+)*)$")


def _reglas(css: str):
    """[(selector, {prop: valor})] en orden de aparición. Los selectores del archivo son
    simples (tag + clases + descendencia); las at-rules quedan aplanadas, que para lo que
    se mide acá (ninguna toca `dim`) da igual."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for m in _REGLA.finditer(css):
        decls = {}
        for trozo in m.group(2).split(";"):
            if ":" in trozo:
                prop, _, val = trozo.partition(":")
                decls[prop.strip().lower()] = val.strip()
        if not decls:
            continue
        for sel in m.group(1).split(","):
            sel = sel.strip()
            if sel and not sel.startswith("@"):
                yield sel, decls


# Cadena de ancestros REAL de una celda del chain (ver el fragmento + pages/options.html):
# el `<td>` cuelga de la tabla `.opt-chain-tbl`, adentro del scroller y del panel. Sin
# modelarla, un selector como `.opt-leg-info .dim` (otra parte de la página) matchearía la
# celda y el test mediría estilos que el navegador nunca le aplica.
_ANCESTROS = (("div", {"opt-panel"}), ("div", {"opt-chain-scroll"}),
              ("table", {"opt-chain-tbl"}), ("tbody", set()), ("tr", set()))


def _compound(txt: str):
    """(tag, {clases}) de un compound simple, o None si tiene sintaxis no modelada."""
    m = _COMPOUND.match(txt)
    if not m:
        return None
    return m.group(1), set(m.group(2).split(".")[1:])


def _matchea(compound, tag: str, clases: set[str]) -> bool:
    sel_tag, sel_clases = compound
    return (not sel_tag or sel_tag == tag) and sel_clases <= clases


def _aplica(sel: str, tag: str, clases: set[str]):
    """Especificidad `(clases, tags)` si el selector alcanza al elemento; None si no.

    Sólo se modelan tag + clases + descendencia (todo lo que usa este archivo). Las
    pseudo-clases se descartan: `:hover` no describe el estado en reposo, que es lo que
    se está midiendo."""
    if any(c in sel for c in (">", "+", "~", "[", "*", ":")):
        return None
    partes = [_compound(x) for x in sel.split()]
    if any(p is None for p in partes):
        return None
    if not _matchea(partes[-1], tag, clases):
        return None
    # los compounds previos tienen que matchear ancestros EN ORDEN (subsecuencia)
    restantes = list(_ANCESTROS)
    for compound in partes[:-1]:
        while restantes and not _matchea(compound, *restantes[0]):
            restantes.pop(0)
        if not restantes:
            return None
        restantes.pop(0)
    return (sum(len(c[1]) for c in partes), sum(1 for c in partes if c[0]))


def _estilo(css: str, tag: str, clases: set[str]) -> dict[str, str]:
    """Cascada resuelta para un elemento `tag` con esas clases, ubicado en la cadena de
    ancestros del chain (misma especificidad → gana el que aparece más abajo, como CSS)."""
    ganadores: dict[str, tuple] = {}
    for orden, (sel, decls) in enumerate(_reglas(css)):
        esp = _aplica(sel, tag, clases)
        if esp is None:
            continue
        for prop, val in decls.items():
            if prop not in ganadores or ganadores[prop][0] <= (esp, orden):
                ganadores[prop] = ((esp, orden), val)
    return {p: v for p, (_, v) in ganadores.items()}


# --------------------------------------------------------------- el matcher, primero --

def test_el_matcher_de_cascada_se_comporta_como_css():
    """El test de abajo vale lo que valga esto: sin control, un matcher que no matchea
    nada declararía "invisible" cualquier cosa (o al revés)."""
    css = """
    td.a { color: rojo; }
    td.a.b { color: verde; }
    .opt-chain-tbl td { padding: 1px; }
    td.c:hover { color: nunca; }
    .opt-leg-info .a { color: otra-parte-de-la-pagina; }
    """
    assert _estilo(css, "td", {"a"}) == {"color": "rojo", "padding": "1px"}
    assert _estilo(css, "td", {"a", "b"})["color"] == "verde"      # más específico gana
    assert "color" not in _estilo(css, "td", {"c"})                # :hover no cuenta
    assert _estilo(css, "th", {"a"}) == {}                         # el tag importa
    # y la descendencia: `.opt-leg-info .a` NO alcanza a una celda del chain
    assert _estilo(css, "td", {"a"})["color"] == "rojo"


# ------------------------------------------------------- la celda sin precio se VE --

@pytest.mark.parametrize("lado", ["C", "V"])
def test_la_celda_sin_precio_utilizable_se_ve_distinta_de_una_operable(lado):
    css = CSS.read_text(encoding="utf-8")
    sin_precio = _estilo(css, "td", {lado, "right", "dim"})
    operable = _estilo(css, "td", {lado, "right", "clickable"})

    distintas = [p for p in _VISUALES if sin_precio.get(p) != operable.get(p)]
    assert distintas, (
        f"la celda sin precio utilizable (td.{lado}.right.dim) resuelve EXACTAMENTE igual "
        f"que una operable (td.{lado}.right.clickable): {sin_precio}. El template la marca "
        "con `dim` + el motivo en el `title`, pero options.css no tiene ninguna regla para "
        "esa clase, así que el aviso es invisible y la única pista es que el click no hace "
        "nada.")


def test_la_celda_sin_precio_tampoco_invita_a_clickear():
    """Complemento del anterior: además de verse distinta, no puede ofrecer el cursor
    de mano. (No alcanza por sí solo — por eso `cursor` no está en `_VISUALES`.)"""
    css = CSS.read_text(encoding="utf-8")
    assert _estilo(css, "td", {"C", "right", "dim"}).get("cursor") != "pointer"


def test_la_fila_sin_contratos_tambien_queda_atenuada():
    """El otro uso de `dim` en el mismo fragmento (la fila "Sin contratos para X"):
    la regla nueva tiene que alcanzarla igual, no ser un caso especial de las puntas."""
    css = CSS.read_text(encoding="utf-8")
    estilo = _estilo(css, "td", {"dim"})
    assert any(p in estilo for p in _VISUALES), (
        f"`td.dim` a secas no resuelve ninguna propiedad visible: {estilo}")


# ------------------------------------------------ ancla: el template sigue marcando --

def _render(call, put):
    from apps.web.templates import TEMPLATES
    return TEMPLATES.env.get_template(FRAGMENTO).render(
        underlying="GGAL", months=["DI"], month="DI", spot=1000.0, n=1, chain_json="[]",
        rows=[{"strike": 100.0, "is_atm": False, "call": call, "put": put}])


def _celdas(html: str):
    return re.findall(r"<td class=\"([^\"]*)\"", html)


def test_el_template_marca_la_celda_sin_precio_y_no_la_hace_clickeable():
    """Ancla de los tests de CSS: si el template dejara de emitir `dim`, medir esa clase
    sería medir el aire."""
    cero = {"ticker": "GFGC100", "bid": 0, "ask": 0, "mid": 0, "last": 0,
            "delta": 0.5, "iv_pct": 40}
    con_punta = {"ticker": "GFGV100", "bid": 1.5, "ask": 2.0, "mid": 1.75, "last": 1.7,
                 "delta": -0.5, "iv_pct": 41}
    html = _render(cero, con_punta)

    clases_call = [c for c in _celdas(html) if "C" in c.split()]
    assert clases_call, "el fragmento dejó de emitir celdas del lado call"
    for c in clases_call:
        assert "dim" in c.split(), f"la punta sin precio no se marcó con `dim`: {c!r}"
        assert "clickable" not in c.split(), f"la punta sin precio quedó clickeable: {c!r}"
    assert "optAddLeg('GFGC100'" not in html, (
        "la celda sin precio utilizable conserva el onclick: armaría la pata al nominal "
        "de 0,01")

    # control: la punta CON precio sigue siendo operable y sin `dim`
    clases_put = [c for c in _celdas(html) if "V" in c.split()]
    assert clases_put and all("clickable" in c.split() and "dim" not in c.split()
                              for c in clases_put), clases_put
