"""Auditoría R1 — todo preset que se RENDERIZA como botón tiene builder en el JS.

`core/domain/options/strategies.py::PRESET_NAMES` publica 10 nombres y
`routers/options.py` los pasa tal cual al template, que dibuja un botón por
nombre. Pero `optBuildPreset` (el builder del cliente, en `pages/options.html`)
definía sólo 8: clickear **Short Call** o **Short Put** devolvía `[]`, o sea
BORRABA la estrategia armada y pisaba el hash compartible de la URL, el 100% de
las veces.

El test del lote anterior (`test_todo_preset_publicado_construye_legs`) verifica
los builders de PYTHON, que sí existían — no cubría este bug. Acá se ata el
contrato REAL: la lista del dominio contra el objeto `presets` del JS.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from core.domain.options.chain import OptionItem
from core.domain.options.greeks import Greeks
from core.domain.options.models import OptionContract
from core.domain.options.rates import ImpliedRates
from core.domain.options.strategies import PRESET_NAMES, preset_legs

_HTML = (Path(__file__).resolve().parents[1]
         / "apps" / "web" / "templates" / "pages" / "options.html")

_BLOCK_RE = re.compile(r"var presets = \{(.*?)\n    \};", re.S)
_NAME_RE = re.compile(r"^\s{6}(\w+): function \(\) \{", re.M)


def _js_presets_block() -> str:
    m = _BLOCK_RE.search(_HTML.read_text(encoding="utf-8"))
    assert m, "no se encontró el objeto `presets` en pages/options.html"
    return m.group(1)


def _js_preset_names() -> set:
    """Nombres definidos en `var presets = { … };` (builders uni y multilínea)."""
    return set(_NAME_RE.findall(_js_presets_block()))


def _js_preset_body(name: str) -> str:
    """Cuerpo de un builder de UNA sola línea (los presets de una pata)."""
    m = re.search(r"^\s{6}" + name + r": function \(\) \{(.*?)\},?$",
                  _js_presets_block(), re.M)
    assert m, f"'{name}' no está definido en una sola línea en optBuildPreset"
    return m.group(1).strip()


def test_el_template_renderiza_la_lista_del_dominio():
    """Guarda de que la lista de botones no se vuelva una copia local."""
    assert "{% for p in presets %}" in _HTML.read_text(encoding="utf-8")
    from apps.web.routers import options as options_router
    assert "PRESET_NAMES" in Path(options_router.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", PRESET_NAMES)
def test_todo_preset_renderizado_tiene_builder_en_el_js(name):
    assert name in _js_preset_names(), (
        f"'{name}' se renderiza como botón pero optBuildPreset no lo define: "
        f"clickearlo devuelve [] y borra la estrategia armada")


def test_el_js_no_define_presets_que_el_dominio_no_publica():
    """La otra dirección: un builder huérfano es código que ningún botón alcanza."""
    assert _js_preset_names() == set(PRESET_NAMES)


@pytest.mark.parametrize("name,kind,factor,qty", [
    ("long_call", "C", "1.02", 1),
    ("long_put", "V", "0.98", 1),
    ("short_call", "C", "1.02", -1),
    ("short_put", "V", "0.98", -1),
    ("covered_call", "C", "1.03", -1),
])
def test_los_builders_de_una_pata_espejan_al_dominio(name, kind, factor, qty):
    """Mismo kind, mismo strike target y mismo signo que
    `core/domain/options/strategies.py::preset_legs`."""
    esperado = 'return [["' + kind + '", s * ' + factor + ", " + str(qty) + "]];"
    assert _js_preset_body(name) == esperado


def _item(kind, strike):
    return OptionItem(
        contract=OptionContract(ticker=f"XX{kind}{int(strike)}DI", root="XX",
                                underlying="XX", kind=kind, strike=strike,
                                month=12, month_code="DI", expiry=date(2026, 12, 18)),
        spot=100.0, bid=1.0, ask=2.0, last=1.5, mid=1.5, volume=0.0,
        open_interest=0.0, pct_change=None, iv=0.4,
        greeks=Greeks(delta=None, gamma=None, theta=None, vega=None, rho=None),
        rates=ImpliedRates(tna_bruta=None, tea_bruta=None, tna_strike=None),
        t_days=100)


def test_los_presets_short_construyen_una_pata_vendida_en_el_dominio():
    """Sanity del lado Python (el JS es su espejo)."""
    chain = [_item(k, float(s)) for k in ("C", "V") for s in range(90, 111, 5)]
    for name in ("short_call", "short_put"):
        legs = preset_legs(name, chain, spot=100.0)
        assert legs and all(leg.qty == -1 for leg in legs)
