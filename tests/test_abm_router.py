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
        assert 'name="ticker"' in r.text and 'name="sheet"' in r.text


def test_abm_form_unknown_sheet():
    with TestClient(app) as c:
        assert c.get("/abm/form?sheet=NOPE").status_code == 400
