"""ABM: backfill de patas de moneda de soberanos + alta de acciones (register_stocks).

(La discovery de tickers de Data912 sin cargar se retiró: el ＋Alta del Universo BYMA
abre el cajón prefilleado y cubre ese flujo.)"""

import pytest

from apps.web.instruments_abm import (
    backfill_soberano_ccy_legs,
    get_instrument,
    list_instruments,
    register_stocks,
)
from config.settings import settings
from core.infrastructure.db import engine as db_engine
from core.infrastructure.db.catalog_repository import ingest_from_excel


@pytest.fixture
def abm_db(tmp_path):
    """Engine apuntado a una .db temporal sembrada del master (no toca prod)."""
    db_engine.configure(tmp_path / "abm_test.db")
    try:
        ingest_from_excel(str(settings.master_xlsx))
        yield
    finally:
        db_engine.configure(settings.catalog_db)


def test_backfill_clona_pata_de_moneda_faltante(abm_db):
    # El master trae AE38 + AE38D, pero NO AE38C. Data912 lista AE38C.
    assert get_instrument("AE38C") is None
    created = backfill_soberano_ccy_legs({"AE38C", "AE38", "AE38D", "ZZ9C"})
    # AE38C clonado; AE38/AE38D ya estaban; ZZ9C no tiene base cargada → no se crea.
    assert created == ["AE38C"]
    leg = get_instrument("AE38C")
    base = get_instrument("AE38")
    assert leg is not None
    assert len(leg["cashflows"]) == len(base["cashflows"]) > 0
    # idempotente
    assert backfill_soberano_ccy_legs({"AE38C"}) == []


def test_backfill_no_inventa_bonos_nuevos(abm_db):
    # Un ticker base sin sufijo (bono nuevo) NO se da de alta por backfill.
    created = backfill_soberano_ccy_legs({"AN29", "CO35"})
    assert created == []


def test_register_stocks_categoria_acciones(abm_db):
    # Las acciones se dan de alta solo-ticker, categoría Acciones, y NO aparecen
    # en la lista editable de bonos del ABM.
    added = register_stocks(["GGAL", "ALUA"])
    assert set(added) == {"GGAL", "ALUA"}
    g = get_instrument("GGAL")
    assert g is not None and g["sheet"] == "Acciones"
    assert not any(e["sheet"] == "Acciones" for e in list_instruments())
    # idempotente
    assert register_stocks(["GGAL"]) == []
