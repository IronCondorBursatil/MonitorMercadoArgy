"""Equivalencia Fase 2: CatalogRepository (SQLite) == ExcelInstrumentsRepository.

Siembra SQLite desde el Excel y verifica que los Instruments reconstruidos sean
idénticos (mismo set de tickers, mismos campos, mismos cashflows módulo el
saneo nan→0.0 de montos).
"""

import math

import pytest

from config.settings import settings
from core.infrastructure.db.catalog_repository import CatalogRepository, ingest_from_excel
from core.infrastructure.repositories import ExcelInstrumentsRepository


def _num(x):
    return 0.0 if (x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))) else float(x)


def _cfs(inst):
    return sorted((cf.date, round(_num(cf.amortization), 9), round(_num(cf.interest), 9))
                  for cf in inst.cashflows)


@pytest.fixture(scope="module")
def repos():
    # re-seed SQLite desde el Excel (intencional: pisa lo que otros tests dejaron
    # en la DB temp de la sesión → override consciente del guard anti-pérdida)
    ingest_from_excel(str(settings.master_xlsx), allow_drop=True)
    excel = ExcelInstrumentsRepository(str(settings.master_xlsx))
    catalog = CatalogRepository(auto_seed=False)
    return excel, catalog


def test_same_ticker_universe(repos):
    excel, catalog = repos
    assert {i.ticker for i in excel.get_all_instruments()} == \
           {i.ticker for i in catalog.get_all_instruments()}
    assert len(catalog.get_all_instruments()) == len(excel.get_all_instruments())


def test_instrument_fields_and_cashflows_match(repos):
    excel, catalog = repos
    ei = {i.ticker: i for i in excel.get_all_instruments()}
    fields = ("short_name", "instrument_type", "maturity_date", "emission_date",
              "cer_base", "cer_lag", "category", "floor_rate_monthly", "spread_rate",
              "cer_spread", "payment_frequency", "day_count")
    mismatches = []
    for t, e in ei.items():
        c = catalog.get_instrument_by_ticker(t)
        assert c is not None, f"{t} missing in catalog"
        for f in fields:
            if getattr(e, f) != getattr(c, f):
                mismatches.append((t, f, getattr(e, f), getattr(c, f)))
        if _cfs(e) != _cfs(c):
            mismatches.append((t, "cashflows", len(e.cashflows), len(c.cashflows)))
    assert not mismatches, mismatches[:15]


def test_by_type_grouping_matches(repos):
    excel, catalog = repos
    types = {i.instrument_type for i in excel.get_all_instruments()}
    for t in types:
        assert {i.ticker for i in excel.get_instruments_by_type(t)} == \
               {i.ticker for i in catalog.get_instruments_by_type(t)}


# ---- protección anti-pérdida de altas DB-only (re-seed destructivo) -------- #

def _fake_inst(ticker, year=2030):
    from datetime import date
    from core.domain.models import Cashflow, Instrument
    return Instrument(ticker=ticker, short_name=ticker, instrument_type="BONAR",
                      maturity_date=date(year, 1, 1),
                      cashflows=[Cashflow(date=date(year, 1, 1), amortization=100.0,
                                          interest=1.0)])


# `tmp_db` (engine aislado a una DB temp + restaurado) vive en conftest.py — compartido.

def test_reseed_refuses_to_drop_db_only_rows(tmp_db):
    """Un re-seed que perdería bonos que viven SOLO en la DB (altas ABM, ej. ONs
    cargadas a mano) debe ABORTAR con la lista de lo que se perdería; con
    allow_drop=True (consciente) procede."""
    from core.infrastructure.db.catalog_repository import reseed_with_meta

    reseed_with_meta([(_fake_inst("AAA1"), "ON", {}), (_fake_inst("BBB2"), "ON", {})])
    # re-seed sin BBB2 → lo perdería → se niega y la DB queda intacta
    with pytest.raises(ValueError, match="BBB2"):
        reseed_with_meta([(_fake_inst("AAA1"), "ON", {})])
    repo = CatalogRepository(auto_seed=False)
    assert repo.get_instrument_by_ticker("BBB2") is not None
    # override explícito → procede
    reseed_with_meta([(_fake_inst("AAA1"), "ON", {})], allow_drop=True)
    repo.reload()
    assert repo.get_instrument_by_ticker("BBB2") is None


# ---- serie_clase (la CLASE de la ON) vive en raw_fields, igual que ley_aplicable -- #

def test_orm_to_domain_reads_serie_clase_from_raw_fields():
    """`_orm_to_domain` expone serie_clase desde raw_fields (display-only, sin columna
    ORM) — alimenta la columna CLASE de la liga ON. Sin DB: objeto ORM transitorio."""
    from core.infrastructure.db.catalog_repository import _orm_to_domain
    from core.infrastructure.db.models import InstrumentORM
    # objeto transitorio (sin flush) → los defaults de columna no se aplican: los fijamos.
    orm = InstrumentORM(ticker="BF37O", short_name="BANCO BBVA ARGENTINA S.A.",
                        instrument_type="HARD DOLLAR", cer_lag=10, payment_frequency=2,
                        day_count="ACT/365.25",
                        raw_fields={"serie_clase": "Clase 37", "ley_aplicable": "Argentina",
                                    "cupon anual %": "8.3"})
    inst = _orm_to_domain(orm)
    assert inst.serie_clase == "Clase 37"
    assert inst.ley_aplicable == "Argentina"   # el patrón hermano sigue intacto
    assert inst.coupon_rate == 8.3             # cupón anual % desde raw_fields


def test_orm_to_domain_serie_clase_absent_is_none():
    from core.infrastructure.db.catalog_repository import _orm_to_domain
    from core.infrastructure.db.models import InstrumentORM
    assert _orm_to_domain(
        InstrumentORM(ticker="X", short_name="Y", instrument_type="CER", cer_lag=10,
                      payment_frequency=2, day_count="ACT/365.25", raw_fields=None)
    ).serie_clase is None


def test_reload_default_does_not_reseed(tmp_db):
    """`reload()` pelado NO debe re-sembrar desde el Excel (pisaría las altas
    DB-only): el default es solo refrescar el cache desde SQLite."""
    from core.infrastructure.db.catalog_repository import reseed_with_meta

    reseed_with_meta([(_fake_inst("SOLO1"), "ON", {})])
    repo = CatalogRepository(auto_seed=False)
    repo.reload()   # sin args: NO toca la DB
    assert repo.get_instrument_by_ticker("SOLO1") is not None
    assert len(repo.get_all_instruments()) == 1   # el Excel (cientos) NO entró
