"""Router de Escenarios/Stress (Fase 4 web). Reprice reusa scenarios.py (testeado)."""

from fastapi.testclient import TestClient

from apps.web.app import app


def test_escenarios_page_renders():
    with TestClient(app) as c:
        r = c.get("/escenarios")
        assert r.status_code == 200
        assert "STRESS" in r.text and 'name="d_tir_bps"' in r.text and 'name="d_fx_pct"' in r.text


def test_escenarios_run():
    with TestClient(app) as c:
        r = c.post("/escenarios/run", data={"d_tir_bps": "100", "d_fx_pct": "10"})
        assert r.status_code == 200
        assert "POR BONO" in r.text and "cartera" in r.text
