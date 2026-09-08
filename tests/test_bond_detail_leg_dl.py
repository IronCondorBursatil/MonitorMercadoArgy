"""Popup de un DUAL_DL_TAMAR: TEA en pesos en la vista base, pata `_TAM` (riel TAMAR como
PURO) y pata `_DL` (TIR en USD del riel dólar-linked como DOLAR_LINKED zero-coupon).
Spec 2026-09-08 §4. Providers stub, sin red; TMVE8 no está en el catálogo de pruebas: el
repo es un doble; el test de router siembra y limpia en el catálogo compartido."""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from apps.web import bond_detail
from apps.web.app import app
from core.domain.models import Cashflow, Instrument, MarketSnapshot
from core.domain.services import FinancialEngine

_EMISION = date(2026, 7, 31)
_VTO = date(2028, 1, 31)
_FX_BASE = 1300.0
_FX = 1500.0
_PRICE = 120.0
_HOY = "2026-09-07"


def _inst(**kw) -> Instrument:
    base = dict(ticker="TMVE8", short_name="Dual DL/TAMAR", instrument_type="DUAL_DL_TAMAR",
                emission_date=_EMISION, maturity_date=_VTO, day_count="30/360",
                fx_base=_FX_BASE, spread_rate=0.0, cashflows=())
    base.update(kw)
    return Instrument(**base)


class _Repo:
    def __init__(self, *insts):
        self._por_ticker = {i.ticker: i for i in insts}

    def get_instrument_by_ticker(self, t):
        return self._por_ticker.get(t)


class _Prov:
    def fetch_snapshots(self, tickers):
        return {t: MarketSnapshot(instrument=None, price=_PRICE) for t in tickers}

    def fetch_historical_prices(self, t, d):
        return {}


class _Idx:
    def get_tamar(self, d=None):
        return 30.0

    @property
    def _cache_tamar(self):
        return {date(2026, 9, 1): 30.0}

    def get_cer(self, d=None):
        return None


class _Fx:
    def get_mayorista_venta(self):
        return _FX


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setenv("MONITOR_AS_OF", _HOY)


def _detail(ticker, repo=None):
    return bond_detail.get_bond_detail(ticker, repo or _Repo(_inst()), _Prov(), _Idx(), _Fx(),
                                       settlement_lag=1)


def test_la_vista_base_publica_tea_en_pesos_tc_inicial_y_las_dos_patas():
    d = _detail("TMVE8")
    assert d is not None and d["ticker"] == "TMVE8"
    settle = bond_detail._resolve_ref(1)
    esperado = FinancialEngine.calculate_tir(MarketSnapshot(instrument=_inst(), price=_PRICE),
                                             _Idx(), _Fx(), settle_date=settle)
    assert d["metrics"]["tir"] == pytest.approx(esperado)
    assert d["meta"]["tc_inicial"] == _FX_BASE
    assert d["meta"]["is_tamar_family"] is True
    assert d["meta"]["cupon"] == "max(TAMAR + 0.000%, dólar-linked)"
    assert d["meta"]["legs"] == [("Riel TAMAR", "TMVE8_TAM"), ("Riel dólar-linked", "TMVE8_DL")]
    # El payback proyectado de la tabla de flujos es el max de rieles (necesita el FX).
    amorts = [r["amortization"] for r in d["cashflows"] if r["amortization"]]
    payoff = FinancialEngine.projected_payoff(_inst(), _Idx(), ref_date=settle, fx_provider=_Fx())
    assert payoff is not None and amorts == [pytest.approx(payoff)]
    assert FinancialEngine.projected_payoff(_inst(), _Idx(), ref_date=settle) is None  # sin FX: None


def test_la_pata_dl_es_la_tir_en_usd_de_un_dolar_linked_zero_coupon():
    d = _detail("TMVE8_DL")
    assert d is not None and d["ticker"] == "TMVE8_DL" and d["meta"]["ticker"] == "TMVE8_DL"
    assert "legs" not in d["meta"]                       # las patas no anidan
    oraculo = Instrument(ticker="TMVE8", short_name="x", instrument_type="DOLAR_LINKED",
                         emission_date=_EMISION, maturity_date=_VTO, day_count="30/360",
                         cashflows=(Cashflow(date=_VTO, amortization=100.0, interest=0.0),))
    settle = bond_detail._resolve_ref(1)
    esperado = FinancialEngine.calculate_tir(MarketSnapshot(instrument=oraculo, price=_PRICE),
                                             _Idx(), _Fx(), settle_date=settle)
    assert esperado is not None
    assert d["metrics"]["tir"] == pytest.approx(esperado)
    assert d["metrics"]["technical_value"] == pytest.approx(100.0 * _FX)   # V.Téc en pesos


def test_la_pata_tam_es_la_tea_del_riel_tamar_solo():
    d = _detail("TMVE8_TAM")
    assert d is not None and d["ticker"] == "TMVE8_TAM"
    puro = _inst(instrument_type="PURO")
    settle = bond_detail._resolve_ref(1)
    esperado = FinancialEngine.calculate_tir(MarketSnapshot(instrument=puro, price=_PRICE),
                                             _Idx(), _Fx(), settle_date=settle)
    assert d["metrics"]["tir"] == pytest.approx(esperado)


def test_dl_fuera_del_tipo_y_tf_sobre_el_tipo_son_tickers_inexistentes():
    al30 = Instrument(ticker="AL30", short_name="AL30", instrument_type="BONAR",
                      maturity_date=date(2030, 7, 9),
                      cashflows=(Cashflow(date=date(2030, 7, 9), amortization=100.0, interest=0.5),))
    repo = _Repo(_inst(), al30)
    assert bond_detail.get_bond_detail("AL30_DL", repo, _Prov(), _Idx(), _Fx()) is None
    assert bond_detail.get_bond_detail("TMVE8_TF", repo, _Prov(), _Idx(), _Fx()) is None
    assert bond_detail.get_bond_detail("TMVE8_DL", _Repo(_inst(maturity_date=None)),
                                       _Prov(), _Idx(), _Fx()) is None


def test_calculadora_y_tir_nominal_m12():
    r = bond_detail.calculate("TMVE8_DL", _Repo(_inst()), _Prov(), _Idx(), _Fx(),
                              mode="from_price", price=_PRICE)
    assert r is not None
    assert bond_detail._nominal_tna(_inst(), 0.30) == pytest.approx(
        FinancialEngine.tea_to_tna_monthly(0.30))


def test_router_renderiza_la_pata_dl_y_los_botones():
    """Siembra TMVE8 en el catálogo compartido y lo limpia SIEMPRE (un DUAL_DL_TAMAR que
    quede ahí rompe la equivalencia con el motor legacy, que no conoce el tipo)."""
    from apps.web.deps import get_repo
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import CashflowORM, InstrumentORM

    init_db()
    # Forzar el auto-seed del singleton `get_repo()` ANTES de insertar TMVE8: si su
    # PRIMERA instanciación (lru_cache) ocurriera con la tabla en 1 fila (sólo TMVE8),
    # `_is_empty()` da False → salta la siembra del Excel → el singleton queda cacheado
    # con UN solo instrumento, y como `reload()` nunca re-siembra, el cleanup de abajo
    # (que borra TMVE8) lo deja en CERO instrumentos para el resto de la sesión de
    # pytest (rompe cualquier test posterior que dependa de `get_repo()` vía TestClient).
    get_repo()
    with SessionLocal.begin() as s:
        orm = InstrumentORM(ticker="TMVE8", short_name="TMVE8", instrument_type="DUAL_DL_TAMAR",
                            sheet="TAMAR", emission_date=_EMISION, maturity_date=_VTO,
                            day_count="30/360", raw_fields={"tipo": "DUAL_DL_TAMAR",
                                                             "tc_inicial": _FX_BASE})
        orm.cashflows = [CashflowORM(ticker="TMVE8", fecha_pago=_VTO, amortizacion=0.0,
                                     cupon_interes=0.0, es_ancla=True)]
        s.add(orm)
    try:
        get_repo().reload()
        with TestClient(app) as c:
            r = c.get("/bond/TMVE8/detail")
            assert r.status_code == 200
            assert "/bond/TMVE8_DL/detail" in r.text and "/bond/TMVE8_TAM/detail" in r.text
            assert "TC inicial" in r.text
            r = c.get("/bond/TMVE8_DL/detail")
            assert r.status_code == 200 and "TMVE8_DL" in r.text
            assert "Riel dólar-linked" not in r.text          # dentro de una pata no se anidan
    finally:
        with SessionLocal.begin() as s:
            s.query(CashflowORM).filter_by(ticker="TMVE8").delete()
            s.query(InstrumentORM).filter_by(ticker="TMVE8").delete()
        get_repo().reload()
