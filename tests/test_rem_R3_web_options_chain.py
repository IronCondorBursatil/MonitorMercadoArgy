"""Remediación R3_web (lote D2) — la chain no puede ofrecer un click que entra a un
precio distinto del que muestra.

`fragments/options_chain.html` renderizaba las cuatro celdas de puntas como
clickeables siempre que existiera el contrato. El cruce importa: la columna BID arma
una pata LONG, que ENTRA AL ASK (y la columna ASK arma una SHORT, que entra al bid).
Con una serie sin oferta —el caso real GFGC7200SE: bid 150,00 / ask 0,00— el usuario
clickeaba un 150,00 bien visible y la pata entraba a mid/last (210,00). El fix del
payoff hizo consistente el número con el servidor, pero la UX que produce el error
seguía igual. Y si NO hay ningún precio utilizable (punta, mid y último en 0) la pata
entra al nominal de 0,01: eso directamente no se puede clickear.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from apps.web.templates import TEMPLATES as _TEMPLATES


def _opt(ticker, kind, strike, bid, ask, last, mid):
    return {"ticker": ticker, "kind": kind, "strike": strike, "bid": bid, "ask": ask,
            "last": last, "mid": mid, "delta": 0.5, "iv_pct": 45.0}


def _render(rows):
    tpl = _TEMPLATES.env.get_template("fragments/options_chain.html")
    return tpl.render(underlying="GGAL", month="SE", months=["SE"], spot=7000.0,
                      n=len(rows), rows=rows, chain_json="[]")


class _Celdas(HTMLParser):
    """(clase, onclick, title, texto) de cada <td>."""

    def __init__(self):
        super().__init__()
        self.tds = []
        self._open = None

    def handle_starttag(self, tag, attrs):
        if tag == "td":
            a = dict(attrs)
            self._open = {"cls": a.get("class", ""), "onclick": a.get("onclick"),
                          "title": a.get("title", ""), "text": ""}

    def handle_data(self, data):
        if self._open is not None:
            self._open["text"] += data.strip()

    def handle_endtag(self, tag):
        if tag == "td" and self._open is not None:
            self.tds.append(self._open)
            self._open = None


def _celdas(rows):
    p = _Celdas()
    p.feed(_render(rows))
    return p.tds


def _por_ticker(tds, ticker, qty):
    """La celda cuyo click arma la pata (ticker, qty), o None si no es clickeable."""
    patron = re.compile(r"optAddLeg\('%s', %d\)" % (ticker, qty))
    return next((t for t in tds if t["onclick"] and patron.search(t["onclick"])), None)


# Caso del incidente: call sin oferta (ask=0) pero con bid/mid/last vivos.
_SIN_OFERTA = _opt("GFGC7200SE", "C", 7200.0, bid=150.0, ask=0.0, last=210.0, mid=210.0)
# Serie muerta: ninguna fuente de precio.
_SIN_NADA = _opt("GFGC9000SE", "C", 9000.0, bid=0.0, ask=0.0, last=0.0, mid=0.0)
# Put con las dos puntas (control).
_NORMAL = _opt("GFGV7000SE", "V", 7000.0, bid=90.0, ask=100.0, last=95.0, mid=95.0)


def test_la_celda_bid_avisa_que_la_pata_long_no_entra_a_esa_punta():
    tds = _celdas([{"strike": 7200.0, "call": _SIN_OFERTA, "put": None, "is_atm": False}])
    celda = _por_ticker(tds, "GFGC7200SE", 1)          # click en BID → pata LONG
    assert celda is not None, "la celda dejó de armar la pata long"
    assert celda["text"].startswith("150.00")
    assert "⚠" in celda["text"], (
        "la celda muestra 150,00 y la pata entra a 210,00 (mid/last) sin ninguna marca")
    assert "210.00" in celda["title"], celda["title"]


def test_la_celda_ask_no_avisa_cuando_la_punta_que_se_usa_existe():
    """La misma serie por el otro lado: la short SÍ entra al bid=150 que hay."""
    tds = _celdas([{"strike": 7200.0, "call": _SIN_OFERTA, "put": None, "is_atm": False}])
    celda = _por_ticker(tds, "GFGC7200SE", -1)         # click en ASK → pata SHORT
    assert celda is not None
    assert "⚠" not in celda["text"], celda
    assert "150.00" in celda["title"], celda["title"]


def test_una_serie_sin_ningun_precio_no_es_clickeable():
    """Sin punta, sin mid y sin último la pata entraría al nominal de 0,01: la celda
    no puede seguir ofreciendo el click."""
    tds = _celdas([{"strike": 9000.0, "call": _SIN_NADA, "put": None, "is_atm": False}])
    assert _por_ticker(tds, "GFGC9000SE", 1) is None, "sigue armando una pata a 0,01"
    assert _por_ticker(tds, "GFGC9000SE", -1) is None
    muertas = [t for t in tds if t["text"] == "0.00" and "clickable" not in t["cls"]]
    assert len(muertas) >= 2, tds


def test_una_serie_con_las_dos_puntas_sigue_clickeable_y_sin_ruido():
    tds = _celdas([{"strike": 7000.0, "call": None, "put": _NORMAL, "is_atm": True}])
    long_ = _por_ticker(tds, "GFGV7000SE", 1)
    short = _por_ticker(tds, "GFGV7000SE", -1)
    assert long_ and short
    assert "⚠" not in long_["text"] and "⚠" not in short["text"]
    # el título dice el precio de ENTRADA de cada lado (ask para long, bid para short)
    assert "100.00" in long_["title"] and "90.00" in short["title"]


def test_el_strike_sin_contrato_sigue_mostrando_un_guion():
    tds = _celdas([{"strike": 8000.0, "call": None, "put": None, "is_atm": False}])
    guiones = [t for t in tds if t["text"] == "—"]
    assert len(guiones) >= 4, tds


@pytest.mark.parametrize("qty,esperado", [(1, "100.00"), (-1, "90.00")])
def test_el_title_espeja_la_regla_del_servidor(qty, esperado):
    """El precio del title tiene que ser el mismo que `_resolve_premium` (servidor) y
    `optEntryPx` (cliente) van a usar."""
    from core.domain.options.analytics import _resolve_premium

    class _Item:
        bid, ask, last, mid = 90.0, 100.0, 95.0, 95.0

    tds = _celdas([{"strike": 7000.0, "call": None, "put": _NORMAL, "is_atm": False}])
    celda = _por_ticker(tds, "GFGV7000SE", qty)
    assert esperado in celda["title"]
    assert f"{_resolve_premium(_Item(), qty):.2f}" == esperado
