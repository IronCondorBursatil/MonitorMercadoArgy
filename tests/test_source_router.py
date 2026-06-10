"""Router /source: status + switch (TestClient corre el lifespan real, loops off)."""

from fastapi.testclient import TestClient

from apps.web.app import app


def test_status_default_byma_open():
    with TestClient(app) as client:
        r = client.get("/source/status")
        assert r.status_code == 200
        j = r.json()
        assert j["active"] == "byma_open"            # default settings.market_source
        modes = {m["mode"] for m in j["modes"]}
        assert {"byma_open", "byma_realtime", "data912"} <= modes


def test_switch_to_data912():
    with TestClient(app) as client:
        r = client.post("/source/select", data={"mode": "data912"})
        assert r.status_code == 200
        assert client.get("/source/status").json()["active"] == "data912"


def test_switch_realtime_without_creds_400(monkeypatch):
    monkeypatch.delenv("BYMADATA_USER", raising=False)
    monkeypatch.delenv("BYMADATA_PASS", raising=False)
    with TestClient(app) as client:
        r = client.post("/source/select", data={"mode": "byma_realtime"})
        assert r.status_code == 400


def test_switch_unknown_mode_400():
    with TestClient(app) as client:
        r = client.post("/source/select", data={"mode": "bogus"})
        assert r.status_code == 400


def test_menu_fragment_renders():
    with TestClient(app) as client:
        r = client.get("/source/menu")
        assert r.status_code == 200
        assert "FUENTE" in r.text
