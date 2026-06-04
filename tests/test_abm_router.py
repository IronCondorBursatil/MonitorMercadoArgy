"""ABM router — sólo lectura (GET). El path de escritura reusa instruments_abm
(ya testeado en test_instruments_abm contra archivos temporales); no se testea
escritura acá para no tocar el master Excel real."""

from fastapi.testclient import TestClient

from apps.web.app import app


def test_abm_page_renders():
    with TestClient(app) as c:
        r = c.get("/abm")
        assert r.status_code == 200
        assert "ABM" in r.text and "INSTRUMENTOS" in r.text


def test_abm_form_for_sheet():
    with TestClient(app) as c:
        r = c.get("/abm/form?sheet=CER")
        assert r.status_code == 200
        assert 'name="ticker_ars"' in r.text and 'name="sheet"' in r.text


def test_abm_form_unknown_sheet():
    with TestClient(app) as c:
        assert c.get("/abm/form?sheet=NOPE").status_code == 400


def test_abm_form_has_calculator():
    """El editor de un bono cargado trae la calculadora precio↔TIR con selector
    de moneda (default a la pata USD en soberanos)."""
    with TestClient(app) as c:
        r = c.get("/abm/form?sheet=Soberanos&key=AL30")
        assert r.status_code == 200
        assert "/abm/calc" in r.text and 'id="abm-calc-tk"' in r.text


def test_abm_calc_price_to_tir():
    with TestClient(app) as c:
        r = c.post("/abm/calc", data={"ticker": "TZXS8", "price": 89.8})
        assert r.status_code == 200
        assert "TIR" in r.text  # fragmento calc_result (dl de métricas)
