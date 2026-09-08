"""Alta de un DUAL_DL_TAMAR por la hoja TAMAR del ABM (fila ancla, `tc_inicial` en
raw_fields → `fx_base`), rechazo sin TC inicial, preview vacío (analítico) y prefill desde
Novedades para títulos públicos TM/TT/TX+letra. Spec 2026-09-08 §5."""
from __future__ import annotations

from datetime import date

import pytest

from apps.web.instruments_abm import (
    SHEET_SCHEMAS, get_instrument, preview_cashflows, save_instrument,
)
from config.settings import settings
from core.infrastructure.db import engine as db_engine
from core.infrastructure.db.catalog_repository import CatalogRepository, init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import BymaCatalogORM, CashflowORM, InstrumentORM

_FIELDS = {
    "ticker_ars": "TMVE8", "tipo": "DUAL_DL_TAMAR", "fecha_emision": "2026-07-31",
    "fecha_vencimiento": "2028-01-31", "base calculo": "30/360", "spread": "0",
    "tc_inicial": "1300.5",
}


@pytest.fixture
def tmp_catalog(tmp_path):
    """Base temporal VACÍA (no combinar con TestClient(app): ver test_perf_W1_cashflows_ancla)."""
    db_engine.configure(tmp_path / "dual_dl.db")
    init_db()
    try:
        yield
    finally:
        db_engine.configure(settings.catalog_db)


def test_la_hoja_tamar_ofrece_el_tipo_y_el_tc_inicial():
    tamar = SHEET_SCHEMAS["TAMAR"]
    tipo = next(f for f in tamar["fields"] if f["key"] == "tipo")
    assert "DUAL_DL_TAMAR" in tipo["options"]
    tc = next(f for f in tamar["fields"] if f["key"] == "tc_inicial")
    assert tc["type"] == "number" and "DUAL_DL_TAMAR" in tc["help"]


def test_el_alta_guarda_solo_el_ancla_y_el_motor_ve_fx_base(tmp_catalog):
    res = save_instrument("TAMAR", dict(_FIELDS), cashflows=None)
    assert res["action"] == "created" and res["ticker"] == "TMVE8"
    with SessionLocal() as s:
        orm = s.get(InstrumentORM, "TMVE8")
        cfs = s.query(CashflowORM).filter_by(ticker="TMVE8").all()
    assert orm.instrument_type == "DUAL_DL_TAMAR" and orm.sheet == "TAMAR"
    assert float(orm.raw_fields["tc_inicial"]) == 1300.5 and orm.day_count == "30/360"
    assert [(c.fecha_pago, c.amortizacion, c.es_ancla) for c in cfs] == [(date(2028, 1, 31), 0.0, True)]
    inst = CatalogRepository(auto_seed=False).get_instrument_by_ticker("TMVE8")
    assert inst is not None and inst.fx_base == 1300.5 and inst.cashflows == ()
    # round-trip del form
    form = get_instrument("TMVE8")
    assert form["sheet"] == "TAMAR" and float(form["fields"]["tc_inicial"]) == 1300.5
    assert form["fields"]["tipo"] == "DUAL_DL_TAMAR" and form["cashflows_source"] == "analitico"


def test_sin_tc_inicial_se_rechaza_con_el_motivo(tmp_catalog):
    for vacio in ("", "0", None):
        campos = {**_FIELDS, "tc_inicial": vacio}
        with pytest.raises(ValueError, match="TC INICIAL"):
            save_instrument("TAMAR", campos, cashflows=None)
    with SessionLocal() as s:
        assert s.get(InstrumentORM, "TMVE8") is None


def test_el_preview_no_propone_schedule_porque_es_analitico():
    out = preview_cashflows(dict(_FIELDS), "TAMAR")
    assert out["cashflows"] == [] and "fórmula cerrada" in out["nota"]


def test_un_schedule_del_form_se_descarta_y_queda_el_ancla(tmp_catalog):
    save_instrument("TAMAR", dict(_FIELDS),
                    cashflows=[{"date": "2028-01-31", "amortization": 150.0, "interest": 0.0}])
    with SessionLocal() as s:
        cfs = s.query(CashflowORM).filter_by(ticker="TMVE8").all()
    assert len(cfs) == 1 and cfs[0].es_ancla


def _fila_universo(symbol, categoria="Títulos Públicos", vencimiento=None):
    return BymaCatalogORM(symbol=symbol, ticker_pesos=symbol, moneda="ARS", cotiza=1,
                          clase_liquidacion="primary", categoria=categoria,
                          security_type="GO", emisor="Gobierno Nacional",
                          vencimiento=vencimiento)


def test_prefill_manda_tm_tt_tx_mas_letra_a_la_hoja_tamar_sin_elegir_tipo(tmp_catalog):
    from core.infrastructure.byma.universe import prefill_for
    with SessionLocal.begin() as s:
        s.add_all([_fila_universo("TMVE8", vencimiento="2028-01-31"),
                   _fila_universo("TTJ26"), _fila_universo("TXMJ8"),
                   _fila_universo("TO26"), _fila_universo("TY30P"),
                   _fila_universo("T15E7"), _fila_universo("S29E7")])
    for sym in ("TMVE8", "TTJ26", "TXMJ8"):
        p = prefill_for(sym)
        assert p is not None and p["sheet"] == "TAMAR", sym
        assert "tipo" not in p["fields"] and p["fields"]["ticker_ars"] == sym
    assert prefill_for("TMVE8")["fields"]["fecha_vencimiento"] == "2028-01-31"
    assert prefill_for("TO26")["sheet"] != "TAMAR"        # BONOFIJA: TO + digito
    assert prefill_for("TY30P")["sheet"] != "TAMAR"       # TY no es TM/TT/TX
    assert prefill_for("T15E7")["sheet"] == "Tasa_Fija"   # BONCAP: T + digito, como antes
    assert prefill_for("S29E7")["sheet"] == "Tasa_Fija"
