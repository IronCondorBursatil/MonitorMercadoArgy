"""Listado ABM de tickers de Data912 ausentes del catálogo (referencia para el alta).

`unknown_data912_tickers` (puro): diff snapshot del hub vs catálogo, agrupado por
endpoint de origen, normalizando el alias `_CER` y excluyendo el set dado
(p.ej. PANEL_LIDER). El endpoint `/abm/data912` lo renderiza en el sidebar del ABM.
"""

import pytest
from fastapi.testclient import TestClient

from apps.web.app import app
from apps.web.instruments_abm import (
    backfill_soberano_ccy_legs,
    get_instrument,
    list_instruments,
    register_stocks,
    unknown_data912_tickers,
)
from config.settings import settings
from core.infrastructure.db import engine as db_engine
from core.infrastructure.db.catalog_repository import ingest_from_excel
from core.infrastructure.schemas import Data912Row


def _row(sym, c):
    return Data912Row(symbol=sym, c=c)


@pytest.fixture
def abm_db(tmp_path):
    """Engine apuntado a una .db temporal sembrada del master (no toca prod)."""
    db_engine.configure(tmp_path / "abm_test.db")
    try:
        ingest_from_excel(str(settings.master_xlsx))
        yield
    finally:
        db_engine.configure(settings.catalog_db)


def test_unknown_grouped_excluye_catalogo_alias_cer_y_exclude():
    snapshot = {
        "AL30": _row("AL30", 70.0),     # en catálogo → fuera
        "AL30C": _row("AL30C", 71.0),   # nuevo (bonds)
        "S2G6D": _row("S2G6D", 95.0),   # nuevo (notes)
        "TXMJ8": _row("TXMJ8", 100.0),  # alias de mercado de TXMJ8_CER (en catálogo) → fuera
        "GGAL": _row("GGAL", 5000.0),   # excluido (panel líder)
        "AER9O": _row("AER9O", 50.0),   # nuevo (corp)
    }
    sources = {"AL30": "bonds", "AL30C": "bonds", "S2G6D": "notes",
               "TXMJ8": "bonds", "GGAL": "stocks", "AER9O": "corp"}
    catalog = {"AL30", "TXMJ8_CER"}

    groups = unknown_data912_tickers(snapshot, sources, catalog, exclude={"GGAL"})

    assert groups["bonds"] == [{"ticker": "AL30C", "price": 71.0}]
    assert groups["notes"] == [{"ticker": "S2G6D", "price": 95.0}]
    assert groups["corp"] == [{"ticker": "AER9O", "price": 50.0}]
    assert "stocks" not in groups  # el único stock (GGAL) estaba excluido


def test_unknown_vacio_si_todo_en_catalogo():
    snap = {"AL30": _row("AL30", 70.0)}
    assert unknown_data912_tickers(snap, {"AL30": "bonds"}, {"AL30"}) == {}


def test_abm_data912_endpoint_ok_y_vacio_sin_hub():
    with TestClient(app) as c:
        r = c.get("/abm/data912")
        assert r.status_code == 200


def test_abm_data912_endpoint_lista_ticker_nuevo():
    with TestClient(app) as c:
        # inyecta un símbolo nuevo en el hub (los loops están off en tests)
        app.state.hub._snap = {"24": {"ZZNEW9": _row("ZZNEW9", 99.0)}, "CI": {}}
        app.state.hub._source = {"ZZNEW9": "bonds"}
        r = c.get("/abm/data912")
        assert r.status_code == 200
        assert "ZZNEW9" in r.text


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
