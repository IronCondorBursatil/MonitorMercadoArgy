"""Persistencia de credenciales BYMA realtime (.env) + endpoints de la UI."""

import os

import pytest
from fastapi.testclient import TestClient

from apps.web.app import app
from config.settings import settings
from core.infrastructure.byma import credentials as cred
from core.infrastructure.byma.sources import BymaRealtimeError, BymaRealtimeSource


def test_save_upsert_and_clear(tmp_path, monkeypatch):
    envp = tmp_path / ".env"
    envp.write_text("# comentario\nOTRA=1\n", encoding="utf-8")
    monkeypatch.delenv("BYMADATA_USER", raising=False)
    monkeypatch.delenv("BYMADATA_PASS", raising=False)

    cred.save_credentials("user1", "pass1", path=envp)
    txt = envp.read_text(encoding="utf-8")
    assert "BYMADATA_USER=user1" in txt and "BYMADATA_PASS=pass1" in txt
    assert "OTRA=1" in txt and "# comentario" in txt          # preserva el resto
    assert os.environ["BYMADATA_USER"] == "user1"             # aplicado en caliente

    cred.save_credentials("user2", "pass2", path=envp)        # upsert, no duplica
    txt2 = envp.read_text(encoding="utf-8")
    assert txt2.count("BYMADATA_USER=") == 1 and "user2" in txt2

    cred.clear_credentials(path=envp)
    assert "BYMADATA" not in envp.read_text(encoding="utf-8")
    assert "BYMADATA_USER" not in os.environ


def test_save_empty_raises(tmp_path):
    with pytest.raises(ValueError):
        cred.save_credentials("", "x", path=tmp_path / ".env")


async def _fake_token_ok(self, stale_token=None):
    self._token = "tok"
    self._expires_at = float("inf")
    return "tok"


async def _fake_token_fail(self, stale_token=None):
    raise BymaRealtimeError("login 401")


def test_credentials_endpoint_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "base_dir", tmp_path)       # .env → tmp (no toca el real)
    monkeypatch.setattr(BymaRealtimeSource, "_ensure_token", _fake_token_ok)  # login mockeado OK
    monkeypatch.delenv("BYMADATA_USER", raising=False)
    monkeypatch.delenv("BYMADATA_PASS", raising=False)
    try:
        with TestClient(app) as client:
            assert client.get("/source/status").json()["realtime_ready"] is False
            # guardar clave (login válido) → activa realtime + escribe .env
            r = client.post("/source/credentials", data={"user": "u", "password": "p"})
            assert r.status_code == 200
            st = client.get("/source/status").json()
            assert st["active"] == "byma_realtime" and st["realtime_ready"] is True
            assert "BYMADATA_USER=u" in (tmp_path / ".env").read_text(encoding="utf-8")
            # borrar → vuelve a byma_open
            client.post("/source/credentials/clear")
            st2 = client.get("/source/status").json()
            assert st2["active"] == "byma_open" and st2["realtime_ready"] is False
    finally:
        os.environ.pop("BYMADATA_USER", None)
        os.environ.pop("BYMADATA_PASS", None)


def test_credentials_endpoint_invalid_login_400(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(BymaRealtimeSource, "_ensure_token", _fake_token_fail)  # login falla
    monkeypatch.delenv("BYMADATA_USER", raising=False)
    monkeypatch.delenv("BYMADATA_PASS", raising=False)
    with TestClient(app) as client:
        r = client.post("/source/credentials", data={"user": "bad", "password": "creds"})
        assert r.status_code == 400
        # NO debe persistir credenciales inválidas
        assert client.get("/source/status").json()["realtime_ready"] is False
        assert not (tmp_path / ".env").exists() or "BYMADATA_USER" not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_credentials_endpoint_blank_400(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.delenv("BYMADATA_USER", raising=False)
    monkeypatch.delenv("BYMADATA_PASS", raising=False)
    with TestClient(app) as client:
        # blanco → 400 antes de tocar la red
        r = client.post("/source/credentials", data={"user": "  ", "password": "  "})
        assert r.status_code == 400
        assert client.get("/source/status").json()["realtime_ready"] is False
