"""Router del popup de detalle (Fase 4 web). TestClient corre el lifespan;
get_bond_detail tolera snapshot None (sin red) → el test no depende de la red."""

from fastapi.testclient import TestClient

from apps.web.app import app
from core.infrastructure.db.catalog_repository import CatalogRepository


def _a_ticker():
    return CatalogRepository(auto_seed=True).get_all_instruments()[0].ticker


def test_bond_detail_renders():
    tk = _a_ticker()
    with TestClient(app) as c:
        r = c.get(f"/bond/{tk}/detail")
        assert r.status_code == 200
        for marker in ("Cashflows", "DV01", "Calculadora", "T+1"):
            assert marker in r.text, marker


def test_bond_detail_unknown_404():
    with TestClient(app) as c:
        assert c.get("/bond/NOPE9/detail").status_code == 404


def test_bond_metrics_calculator():
    tk = _a_ticker()
    with TestClient(app) as c:
        r = c.post(f"/bond/{tk}/metrics", data={"settlement_lag": "1", "price": "100"})
        assert r.status_code == 200 and "TIR" in r.text
        r2 = c.post(f"/bond/{tk}/metrics", data={"settlement_lag": "1", "tir_pct": "30"})
        assert r2.status_code == 200 and "Precio dirty" in r2.text
