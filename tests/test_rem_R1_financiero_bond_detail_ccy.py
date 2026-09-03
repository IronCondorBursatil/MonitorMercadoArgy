"""Auditoría R1 — "¿cotiza en USD?" tiene UNA sola definición.

El hallazgo #2 pedía unificar las copias divergentes del concepto. Quedó sin
tocar la TERCERA: `apps/web/bond_detail.py::_is_usd_quoted`, que decidía la
etiqueta de moneda del popup de detalle con una lista propia de tipos + sufijo
`D`. Diferencias medidas contra `core.domain.portfolio.position_currency`
(la fuente única, la misma que usan cartera / escenarios / curva):

  · las patas **CABLE** (…C) de BONAR/GLOBAL/BOPREAL salían rotuladas **ARS**
    (AL30C cotiza en dólar cable, no en pesos);
  · **PROVINCIAL HARD DOLLAR** no estaba en la lista → BA37D/BB37D/BC37D/CH24D/
    CO27D/SA24D (6 especies en la db viva) salían rotuladas **ARS**;
  · el matcheo era por igualdad exacta de `instrument_type`, así que cualquier
    variante nueva del tipo se caía en silencio.
"""
from __future__ import annotations

from datetime import date

import pytest

from apps.web.bond_detail import _is_usd_quoted
from core.domain.models import Instrument
from core.domain.portfolio import position_currency


def _inst(ticker: str, itype: str) -> Instrument:
    return Instrument(ticker=ticker, short_name=ticker, instrument_type=itype,
                      maturity_date=date(2030, 1, 1), cashflows=[])


# (ticker, tipo, ¿cotiza en USD?)
_CASOS = [
    ("AL30D", "BONAR", True),
    ("AL30C", "BONAR", True),      # ← antes ARS
    ("AL30", "BONAR", False),
    ("GD30D", "GLOBAL", True),
    ("GD30C", "GLOBAL", True),     # ← antes ARS
    ("BPOA7D", "BOPREAL", True),
    ("BPOA7C", "BOPREAL", True),   # ← antes ARS
    ("BPOA7", "BOPREAL", False),
    ("BA37D", "PROVINCIAL HARD DOLLAR", True),   # ← antes ARS
    ("CO32", "PROVINCIAL HARD DOLLAR", False),
    ("YMCHD", "HARD DOLLAR", True),
    ("YMCHO", "HARD DOLLAR", False),
    ("VSCKD", "DOLLAR LINKED", True),
    ("VSCKO", "DOLLAR LINKED", False),
    ("TZV26", "DOLAR_LINKED", False),            # DL soberano: cotiza en pesos
    ("S30S6", "LECAP", False),
    ("TX28", "CER", False),
    ("GGAL", "ACCION", False),
]


@pytest.mark.parametrize("ticker,itype,esperado", _CASOS)
def test_is_usd_quoted_coincide_con_position_currency(ticker, itype, esperado):
    inst = _inst(ticker, itype)
    assert _is_usd_quoted(inst) is esperado
    assert (position_currency(itype, ticker) == "USD") is esperado


def test_las_patas_cable_se_rotulan_usd_en_el_popup():
    """El caso que el helper viejo daba mal: …C es dólar cable, no pesos."""
    assert _is_usd_quoted(_inst("AL30C", "BONAR")) is True


def test_las_provinciales_hard_dollar_se_rotulan_usd():
    """Tipo que ni figuraba en la lista propia del helper viejo."""
    assert _is_usd_quoted(_inst("BA37D", "PROVINCIAL HARD DOLLAR")) is True


def test_no_queda_una_lista_propia_de_tipos_en_bond_detail():
    """Guarda estructural: `_is_usd_quoted` delega, no re-enumera los tipos."""
    import inspect

    from apps.web import bond_detail
    src = inspect.getsource(bond_detail._is_usd_quoted)
    assert "position_currency" in src
    assert "BONAR" not in src and "BOPREAL" not in src
