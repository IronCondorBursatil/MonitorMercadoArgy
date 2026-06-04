"""Multi-ticker: 1 instrumento puede tener hasta 3 tickers (pesos/MEP/cable).

Modelo: el master/SQLite guardan UNA fila por bono con ticker (primario) +
ticker_mep + ticker_ccl; al cargar se EXPANDE a una especie por ticker
(comparten términos y flujos) → el pricing y los paneles no cambian, y cada
ticker linkea con su precio de Data912 por separado.
"""

from datetime import date

import pytest

from config.settings import settings
from core.domain.models import Cashflow, Instrument
from core.infrastructure.db import engine as db_engine
from core.infrastructure.db.catalog_repository import CatalogRepository, reseed_with_meta
from core.infrastructure.repositories import _currency_tickers, expand_currency_legs


def test_currency_tickers_nuevo_esquema_y_fallback():
    assert _currency_tickers({"ticker_ars": "AL30", "ticker_mep": "AL30D", "ticker_ccl": "AL30C"}) \
        == ["AL30", "AL30D", "AL30C"]
    # huecos: solo MEP + CABLE
    assert _currency_tickers({"ticker_ars": "", "ticker_mep": "BPA7D", "ticker_ccl": "BPA7C"}) \
        == ["BPA7D", "BPA7C"]
    # fallback al esquema viejo (ticker único)
    assert _currency_tickers({"ticker": "TX26"}) == ["TX26"]
    # dedupe + upper/strip
    assert _currency_tickers({"ticker_ars": " al30 ", "ticker_mep": "AL30"}) == ["AL30"]


def test_expand_currency_legs_comparte_terminos():
    cfs = [Cashflow(date=date(2030, 7, 9), amortization=100.0, interest=3.0)]
    primary = Instrument(ticker="AL30", short_name="AL30", instrument_type="BONAR",
                         maturity_date=date(2030, 7, 9), cashflows=cfs)
    legs = expand_currency_legs(primary, ["AL30D", "AL30C", "AL30"])  # AL30 repetido → dedupe
    assert [i.ticker for i in legs] == ["AL30", "AL30D", "AL30C"]
    for leg in legs:
        assert leg.instrument_type == "BONAR"
        assert leg.maturity_date == date(2030, 7, 9)
        assert [(c.date, c.total) for c in leg.cashflows] == [(date(2030, 7, 9), 103.0)]


@pytest.fixture
def tmp_db(tmp_path):
    db_engine.configure(tmp_path / "mt.db")
    try:
        yield
    finally:
        db_engine.configure(settings.catalog_db)


def test_catalog_expande_una_fila_en_tres_especies(tmp_db):
    cfs = [Cashflow(date=date(2030, 7, 9), amortization=100.0, interest=3.0)]
    primary = Instrument(ticker="AL30", short_name="AL30", instrument_type="BONAR",
                         maturity_date=date(2030, 7, 9), cashflows=cfs)
    # 1 fila por bono con sus tickers secundarios
    reseed_with_meta([(primary, "Soberanos", {"ticker_ars": "AL30"}, ["AL30D", "AL30C"])])

    repo = CatalogRepository(auto_seed=False)
    insts = repo.get_all_instruments()
    assert sorted(i.ticker for i in insts) == ["AL30", "AL30C", "AL30D"]
    assert repo.get_instrument_by_ticker("AL30D").maturity_date == date(2030, 7, 9)
    assert repo.get_instrument_by_ticker("AL30C").instrument_type == "BONAR"
    # todos comparten los flujos del bono
    assert len(repo.get_instrument_by_ticker("AL30C").cashflows) == 1
