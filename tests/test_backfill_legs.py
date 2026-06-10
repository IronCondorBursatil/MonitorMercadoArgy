"""Backfill de patas cotizantes faltantes deduciendo el grupo por el universo BYMA.

Solo soberanos + ON (paneles ccy-filter); los instrumentos pesos (CER/Tasa Fija/...)
NO se tocan (serían fila basura). Reusa save_instrument (consolida + re-keya)."""

from datetime import date

from config.settings import settings
from core.domain.models import Instrument
from core.infrastructure.db import engine as db_engine
from core.infrastructure.db.catalog_repository import init_db, instrument_to_orm
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import BymaCatalogORM, CashflowORM, InstrumentORM


def _uni(symbol, moneda, tp, cotiza=1, segmento=None, isin=None):
    return BymaCatalogORM(symbol=symbol, moneda=moneda, ticker_pesos=tp, cotiza=cotiza,
                          segmento=segmento, isin=isin, categoria="Obligaciones Negociables")


def _seed_universe(s, rows):
    for r in rows:
        s.add(r)


def test_backfill_adds_missing_cable_leg(tmp_path):
    from apps.web.instruments_abm import backfill_legs_from_universe, get_instrument
    db_engine.configure(tmp_path / "b.db")
    try:
        init_db()
        with SessionLocal.begin() as s:
            s.execute(__import__("sqlalchemy").delete(CashflowORM))
            s.execute(__import__("sqlalchemy").delete(InstrumentORM))
            s.execute(__import__("sqlalchemy").delete(BymaCatalogORM))
            # universo: grupo XX1 cotiza en pesos(O)/MEP(D)/cable(C)
            _seed_universe(s, [
                _uni("XX1O", "ARS", "XX1", isin="ARTEST000001"),
                _uni("XX1D", "MEP", "XX1", isin="ARTEST000001"),
                _uni("XX1C", "cable", "XX1", isin="ARTEST000001"),
                _uni("XX1X", "ARS", "XX1", cotiza=0),          # no cotiza → no se agrega
                _uni("XX1B", "cable", "XX1", segmento="SB"),   # SENEBI → no se agrega
            ])
            # ON curada con O + D, FALTA cable
            s.add(instrument_to_orm(
                Instrument(ticker="XX1O", short_name="ON X", instrument_type="HARD DOLLAR"),
                sheet="Obligaciones_Negociables", ticker_mep="XX1D"))

        plan = backfill_legs_from_universe(dry_run=True)
        assert any(p["ticker"] == "XX1O" and p["added"] == ["XX1C"] for p in plan)

        res = backfill_legs_from_universe(dry_run=False)
        assert any(r["ticker"] == "XX1O" for r in res)
        inst = get_instrument("XX1O")
        legs = {inst["fields"].get(k) for k in ("ticker_ars", "ticker_mep", "ticker_ccl")}
        assert legs == {"XX1O", "XX1D", "XX1C"}     # cable agregada; X/SB NO

        # idempotente
        assert backfill_legs_from_universe(dry_run=True) == []
    finally:
        db_engine.configure(settings.catalog_db)


def test_backfill_links_different_base_by_isin(tmp_path):
    """Linkea patas de BASE distinta por ISIN (caso BOPREAL: BPC7D/BPC7C ↔ BPOC7)."""
    from apps.web.instruments_abm import backfill_legs_from_universe, get_instrument
    from sqlalchemy import delete
    db_engine.configure(tmp_path / "b3.db")
    try:
        init_db()
        with SessionLocal.begin() as s:
            s.execute(delete(CashflowORM))
            s.execute(delete(InstrumentORM))
            s.execute(delete(BymaCatalogORM))
            # MEP/cable bajo base "PFX"; pesos bajo base DISTINTA "PFXO"; MISMO ISIN
            s.add(BymaCatalogORM(symbol="PFXD", moneda="MEP", ticker_pesos="PFX",
                                 cotiza=1, isin="ARISIN0001"))
            s.add(BymaCatalogORM(symbol="PFXC", moneda="cable", ticker_pesos="PFX",
                                 cotiza=1, isin="ARISIN0001"))
            s.add(BymaCatalogORM(symbol="PFXO", moneda="ARS", ticker_pesos="PFXO",
                                 cotiza=1, isin="ARISIN0001"))
            # curado: solo MEP + cable, con el ISIN del activo
            s.add(instrument_to_orm(
                Instrument(ticker="PFXD", short_name="BOPREAL", instrument_type="BOPREAL",
                           isin="ARISIN0001"),
                sheet="Soberanos", ticker_ccl="PFXC"))
        res = backfill_legs_from_universe(dry_run=False)
        assert any(r["ticker"] == "PFXD" and r["added"] == ["PFXO"] for r in res)
        inst = get_instrument("PFXD")  # se re-keyó a PFXO primario, pero se busca por pata
        legs = {inst["fields"].get(k) for k in ("ticker_ars", "ticker_mep", "ticker_ccl")}
        assert legs == {"PFXO", "PFXD", "PFXC"}   # pesos de base distinta, linkeado por ISIN
    finally:
        db_engine.configure(settings.catalog_db)


def test_backfill_skips_peso_instruments(tmp_path):
    """CER/Tasa Fija/etc NO se tocan aunque el universo tenga sus patas D/C."""
    from apps.web.instruments_abm import backfill_legs_from_universe
    from sqlalchemy import delete
    db_engine.configure(tmp_path / "b2.db")
    try:
        init_db()
        with SessionLocal.begin() as s:
            s.execute(delete(CashflowORM))
            s.execute(delete(InstrumentORM))
            s.execute(delete(BymaCatalogORM))
            _seed_universe(s, [
                _uni("TX26", "ARS", "TX26"), _uni("TX26D", "MEP", "TX26"),
                _uni("TX26C", "cable", "TX26"),
            ])
            s.add(instrument_to_orm(
                Instrument(ticker="TX26", short_name="CER", instrument_type="BONCER"),
                sheet="CER"))
        # la hoja CER no está en _MULTI_CCY_SHEETS → sin candidatos
        assert backfill_legs_from_universe(dry_run=True) == []
    finally:
        db_engine.configure(settings.catalog_db)
