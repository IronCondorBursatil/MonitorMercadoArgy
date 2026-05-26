"""Smoke de páginas read-only BCRA y Cashflows (Fase 4 web)."""

from fastapi.testclient import TestClient

from apps.web.app import app


def test_bcra_page_renders():
    with TestClient(app) as c:
        r = c.get("/bcra")
        assert r.status_code == 200
        assert "RESERVAS" in r.text and "TAMAR" in r.text and "A3500" in r.text


def test_cashflows_page_renders():
    with TestClient(app) as c:
        r = c.get("/cashflows")
        assert r.status_code == 200
        assert "CASHFLOWS" in r.text
