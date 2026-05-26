"""Router de Cartera (Fase 4 web). GET es read-only; el round-trip CRUD usa un
ticker sintético y limpia siempre (try/finally) para no tocar tenencias reales."""

from fastapi.testclient import TestClient

from apps.web.app import app


def test_cartera_page_renders():
    with TestClient(app) as c:
        r = c.get("/cartera")
        assert r.status_code == 200
        assert "POSICIONES" in r.text and "AGREGAR" in r.text


def test_cartera_crud_roundtrip():
    with TestClient(app) as c:
        try:
            r = c.post("/cartera/holding",
                       data={"ticker": "ZZTEST", "nominal": "1000", "cost_price": "50"})
            assert r.status_code == 200 and "ZZTEST" in r.text
        finally:
            d = c.request("DELETE", "/cartera/holding/ZZTEST")
            assert d.status_code == 200
            assert "ZZTEST" not in d.text
