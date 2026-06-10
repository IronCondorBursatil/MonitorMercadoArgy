"""Smoke tests del router de catálogo: página + fragmentos de familias (con los
fetchers live monkeypatcheados → sin red) + ficha desde la DB aislada."""
from __future__ import annotations

import os

os.environ.setdefault("MONITOR_DISABLE_LOOPS", "1")

import pytest
from fastapi.testclient import TestClient

from apps.web.app import app
from core.infrastructure.byma import catalog_products as cp
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import InstrumentORM


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_catalogo_page_200(client):
    r = client.get("/catalogo")
    assert r.status_code == 200
    assert "Catálogo de Productos" in r.text


def test_indices_fragment_renders(client, monkeypatch):
    monkeypatch.setattr(cp, "index_snapshot", lambda *a, **k: [
        {"code": "M", "name": "S&P MERVAL", "last": 3084616.0, "prev": 3000000.0,
         "change_pct": 2.82, "points": [1.0, 2.0, 3.0]},
    ])
    cp._CACHE.pop("indices", None)
    r = client.get("/catalogo/indices")
    assert r.status_code == 200
    assert "MERVAL" in r.text and "+2.82%" in r.text


def test_cauciones_fragment_renders(client, monkeypatch):
    monkeypatch.setattr(cp, "cauciones_cached", lambda *a, **k: [
        {"symbol": "PESOS-0907-U-CT-ARS", "underlying": "PESOS", "ccy": "ARS",
         "days": 1, "maturity": "2026-06-09", "rate": 0.35, "volume": 1e9},
    ])
    r = client.get("/catalogo/cauciones")
    assert r.status_code == 200
    assert "PESOS-0907-U-CT-ARS" in r.text


def test_senebi_fragment_filters(client, monkeypatch):
    monkeypatch.setattr(cp, "senebi_on_cached", lambda *a, **k: [
        {"symbol": "A11LD.SB", "ccy": "EXT", "maturity": "2028-03-31", "days": 666, "last": 99.0},
        {"symbol": "ZZZ.SB", "ccy": "ARS", "maturity": "2027-01-01", "days": 300, "last": 50.0},
    ])
    r = client.get("/catalogo/senebi?q=A11")
    assert r.status_code == 200
    assert "A11LD.SB" in r.text and "ZZZ.SB" not in r.text


def test_ficha_fragment_from_db(client):
    init_db()
    with SessionLocal.begin() as s:
        s.merge(InstrumentORM(ticker="AL30FX", isin="X9", short_name="t",
                              raw_fields={"byma": {"ficha": {"ley": "Nacional",
                                                             "moneda": "Dólares"}}}))
    r = client.get("/catalogo/ficha?ticker=AL30FX")
    assert r.status_code == 200
    assert "Ficha técnica" in r.text and "Nacional" in r.text


def test_ficha_fragment_by_isin(client):
    init_db()
    with SessionLocal.begin() as s:
        s.merge(InstrumentORM(ticker="ONX", isin="AR0271987817", short_name="t",
                              raw_fields={"byma": {"ficha": {"ley": "Nacional"}}}))
    r = client.get("/catalogo/ficha?ticker=AR0271987817")   # busca por ISIN
    assert r.status_code == 200 and "Nacional" in r.text
