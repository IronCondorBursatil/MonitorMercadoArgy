"""Enriquecimiento de ISIN desde el catálogo BYMA (DB temporal aislada)."""

import csv

from sqlalchemy import delete, select

from config.settings import settings
from core.domain.models import Instrument
from core.infrastructure.byma.catalog_enrich import enrich_isin_from_byma
from core.infrastructure.db import engine as db_engine
from core.infrastructure.db.catalog_repository import init_db, instrument_to_orm
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import BymaCatalogORM, CashflowORM, InstrumentORM

_HEADER = ["symbol", "ticker_pesos", "moneda", "securityType", "sufijo",
           "clase_liquidacion", "segmento", "cotiza", "panel", "codigoIsin",
           "tipoEspecie", "insType", "emisor"]


def _write_catalog(path):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(_HEADER)
        w.writerow(["AL30D", "AL30", "USD", "GO", "D", "primary", "", "True",
                    "Titulos Publicos", "ARARGE3209S6", "Titulos Publicos", "BOND",
                    "REP. ARGENTINA"])
        w.writerow(["GGAL", "GGAL", "ARS", "CS", "L", "primary", "", "True",
                    "Acciones Lideres", "ARP495251018", "Acciones", "EQUITY",
                    "GRUPO FINANCIERO GALICIA S.A."])


def _seed_catalog():
    init_db()
    with SessionLocal.begin() as s:
        s.execute(delete(CashflowORM))
        s.execute(delete(InstrumentORM))
        s.add(instrument_to_orm(
            Instrument(ticker="AL30", short_name="AL30", instrument_type="BONAR"),
            sheet="Soberanos", ticker_mep="AL30D", ticker_ccl="AL30C"))
        s.add(instrument_to_orm(
            Instrument(ticker="GGAL", short_name="GGAL", instrument_type="ACCION"),
            sheet="Acciones"))


def test_enrich_sets_isin(tmp_path):
    db_engine.configure(tmp_path / "isin.db")  # DB aislada
    try:
        _seed_catalog()
        csvp = tmp_path / "cat.csv"
        _write_catalog(csvp)

        n = enrich_isin_from_byma(csvp)
        assert n == 2

        with SessionLocal() as s:
            al30 = s.get(InstrumentORM, "AL30")
            assert al30.isin == "ARARGE3209S6"          # matcheado por la pata AL30D
            assert (al30.raw_fields or {}).get("byma", {}).get("emisor") == "REP. ARGENTINA"
            ggal = s.get(InstrumentORM, "GGAL")
            assert ggal.isin == "ARP495251018"

        # idempotente: 2da corrida no reescribe nada
        assert enrich_isin_from_byma(csvp) == 0
    finally:
        db_engine.configure(settings.catalog_db)  # restaurar engine compartido


def test_enrich_missing_csv_returns_zero(tmp_path):
    db_engine.configure(tmp_path / "isin2.db")
    try:
        _seed_catalog()
        assert enrich_isin_from_byma(tmp_path / "nope.csv") == 0
    finally:
        db_engine.configure(settings.catalog_db)


def test_get_instrument_exposes_isin_from_column(tmp_path):
    """El form del ABM muestra el ISIN de la COLUMNA (no de raw_fields)."""
    from apps.web.instruments_abm import get_instrument
    db_engine.configure(tmp_path / "f.db")
    try:
        init_db()
        with SessionLocal.begin() as s:
            s.execute(delete(CashflowORM))
            s.execute(delete(InstrumentORM))
            s.add(instrument_to_orm(
                Instrument(ticker="AL30", short_name="AL30", instrument_type="BONAR",
                           isin="ARARGE3209S6"),
                sheet="Soberanos", ticker_mep="AL30D"))
        assert get_instrument("AL30")["fields"]["isin"] == "ARARGE3209S6"
        # también desde la pata MEP (mismo bono)
        assert get_instrument("AL30D")["fields"]["isin"] == "ARARGE3209S6"
    finally:
        db_engine.configure(settings.catalog_db)


def test_get_instrument_isin_fallback_to_byma_catalog(tmp_path):
    """Si el instrumento curado no tiene isin, el form lo toma del universo BYMA."""
    from apps.web.instruments_abm import get_instrument
    db_engine.configure(tmp_path / "f2.db")
    try:
        init_db()
        with SessionLocal.begin() as s:
            s.execute(delete(CashflowORM))
            s.execute(delete(InstrumentORM))
            s.add(instrument_to_orm(
                Instrument(ticker="GD30", short_name="GD30", instrument_type="GLOBAL"),
                sheet="Soberanos", ticker_ccl="GD30C"))
            s.add(BymaCatalogORM(symbol="GD30C", isin="US040114HS26"))  # pata en el universo
        assert get_instrument("GD30")["fields"]["isin"] == "US040114HS26"
    finally:
        db_engine.configure(settings.catalog_db)
