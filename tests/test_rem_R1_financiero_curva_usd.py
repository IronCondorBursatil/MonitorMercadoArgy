"""Auditoría R1 — la curva `soberanos_usd` cuenta cada bono UNA vez (pata MEP).

CONSECUENCIA DEL FIX #2 (`position_currency` pasó a devolver "USD" también para
las patas CABLE): `/curva/data?grupo=soberanos_usd` filtraba por
`position_currency(...) == "USD"`, así que a partir de ese fix cada BONAR/GLOBAL/
BOPREAL con las dos especies aportaba DOS puntos casi idénticos al ajuste
logarítmico (≈21 → ≈42 en la db viva), duplicando su peso frente a los sólo-MEP
(AO27D/AO28D) y dibujando puntos superpuestos en el gráfico.

La curva USD del panel es la de **MEP** (dólar bolsa), que es la que cotiza el
mercado local: el filtro correcto es `position_fx_leg(...) == "MEP"`.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from apps.web.routers import curva as curva_router
from core.domain.models import Instrument, MarketSnapshot
from core.domain.portfolio import position_currency, position_fx_leg


def _metric(ticker: str, itype: str, md: float, tir: float):
    inst = Instrument(
        ticker=ticker, short_name=ticker, instrument_type=itype,
        maturity_date=date(2030, 1, 1), cashflows=[],
    )
    return SimpleNamespace(
        snapshot=MarketSnapshot(instrument=inst, price=70.0), duration=md, tir=tir,
    )


class _State:
    def __init__(self, metrics):
        self._m = metrics

    def metrics(self):
        return self._m


_UNIVERSO = [
    _metric("AL30D", "BONAR", 2.0, 0.10),    # MEP  → entra
    _metric("AL30C", "BONAR", 2.01, 0.101),  # CABLE → NO entra (duplica AL30D)
    _metric("AL30", "BONAR", 2.0, 0.10),     # ARS  → NO entra
    _metric("GD35D", "GLOBAL", 8.0, 0.12),
    _metric("GD35C", "GLOBAL", 8.02, 0.121),
    _metric("BPOA7D", "BOPREAL", 1.0, 0.09),
    _metric("BPOA7C", "BOPREAL", 1.01, 0.091),
    _metric("AO27D", "BONAR", 1.5, 0.11),    # sólo-MEP (no tiene pata cable)
]


def _points(grupo="soberanos_usd"):
    data = curva_router.curva_data(grupo=grupo, state=_State(_UNIVERSO))
    import json
    return json.loads(data.body)["points"]


def _tickers(grupo="soberanos_usd"):
    return [p["ticker"] for p in _points(grupo)]


def test_la_curva_usd_no_duplica_cada_bono_con_su_pata_cable():
    tks = _tickers()
    assert set(tks) == {"AL30D", "GD35D", "BPOA7D", "AO27D"}
    assert not [t for t in tks if t.endswith("C")], f"entró una pata CABLE: {tks}"
    assert len(tks) == len(set(tks))


def test_los_puntos_salen_ordenados_por_x():
    """El fit logarítmico toma `points[0].x` / `points[-1].x` como extremos del
    dominio: si la lista no viniera ordenada por x, la curva de ajuste se
    dibujaría sobre un rango arbitrario.

    (La línea que estaba acá antes —`tks == sorted(tks, key=lambda t: 0)`— era
    TAUTOLÓGICA: `sorted` es estable y con clave constante devuelve la misma
    lista, así que no podía fallar nunca.)
    """
    pts = _points()
    xs = [p["x"] for p in pts]
    assert xs == sorted(xs), f"los puntos no vienen por MD creciente: {xs}"
    # anclas del escenario: MD 1.0 / 1.5 / 2.0 / 8.0 en ese orden
    assert [p["ticker"] for p in pts] == ["BPOA7D", "AO27D", "AL30D", "GD35D"]
    assert xs == [1.0, 1.5, 2.0, 8.0]
    assert xs != sorted(xs, reverse=True), "el escenario no distingue el orden"


def test_un_bono_aporta_un_solo_punto_al_ajuste():
    """El peso de AL30 en el fit no puede ser el doble que el de AO27D."""
    tks = _tickers()
    assert sum(1 for t in tks if t.startswith("AL30")) == 1
    assert sum(1 for t in tks if t.startswith("AO27")) == 1


def test_el_filtro_es_la_pata_mep_no_la_moneda():
    """Guarda del concepto: con `position_currency` las dos patas dan "USD"; lo
    que las distingue es `position_fx_leg`."""
    assert position_currency("BONAR", "AL30C") == "USD"
    assert position_currency("BONAR", "AL30D") == "USD"
    assert position_fx_leg("BONAR", "AL30C") == "CABLE"
    assert position_fx_leg("BONAR", "AL30D") == "MEP"


@pytest.mark.parametrize("grupo", ["cer", "tasa_fija", "tamar"])
def test_los_grupos_en_pesos_no_filtran_por_pata(grupo):
    """Sólo `soberanos_usd` tiene filtro de pata; el resto no debe perder filas."""
    assert curva_router._CURVA_GROUPS[grupo][2] is None
