"""Equivalencia Fase 2: CatalogRepository (SQLite) == ExcelInstrumentsRepository.

Siembra SQLite desde el Excel y verifica que los Instruments reconstruidos sean
idénticos (mismo set de tickers, mismos campos, mismos cashflows módulo el
saneo nan→0.0 de montos).
"""

import math

import pytest

from config.settings import MASTER_XLSX
from core.infrastructure.db.catalog_repository import CatalogRepository, ingest_from_excel
from core.infrastructure.repositories import ExcelInstrumentsRepository


def _num(x):
    return 0.0 if (x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))) else float(x)


def _cfs(inst):
    return sorted((cf.date, round(_num(cf.amortization), 9), round(_num(cf.interest), 9))
                  for cf in inst.cashflows)


@pytest.fixture(scope="module")
def repos():
    ingest_from_excel(MASTER_XLSX)  # re-seed SQLite desde el Excel
    excel = ExcelInstrumentsRepository(MASTER_XLSX)
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
