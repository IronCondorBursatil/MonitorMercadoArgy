"""Router del panel FCI (CAFCI). Lee del CAFCIProvider (hidrata de disco)."""

from fastapi.testclient import TestClient

from apps.web.app import app


def test_fci_page_renders():
    with TestClient(app) as c:
        r = c.get("/fci")
        assert r.status_code == 200
        assert "FCI" in r.text and 'name="tipo"' in r.text and 'name="metric"' in r.text


def test_fci_rows():
    with TestClient(app) as c:
        r = c.get("/fci/rows?metric=tna&sort=mes_1")
        assert r.status_code == 200
        assert "VCP" in r.text and "Sociedad" in r.text
