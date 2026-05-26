"""Router de la curva TIR vs MD (Chart.js). El fit reusa panels._fit_log_curve."""

from fastapi.testclient import TestClient

from apps.web.app import app


def test_curva_page_renders():
    with TestClient(app) as c:
        r = c.get("/curva")
        assert r.status_code == 200
        assert "curva-canvas" in r.text and "chart.js" in r.text.lower() and "CURVA" in r.text


def test_curva_data_shape():
    with TestClient(app) as c:
        d = c.get("/curva/data?grupo=cer").json()
        assert {"points", "fit", "label"} <= set(d)
        assert isinstance(d["points"], list) and isinstance(d["fit"], list)
        # grupo desconocido → fallback a soberanos_usd (200, no error)
        assert c.get("/curva/data?grupo=NOPE").status_code == 200
