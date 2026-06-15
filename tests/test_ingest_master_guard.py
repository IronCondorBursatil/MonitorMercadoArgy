"""ingest_master: guards anti-corrupción — server vivo, altas DB-only y red de seguridad.

El re-seed Excel→SQLite es DESTRUCTIVO (wipe + seed). Estos tests fijan que:
  · con el monitor corriendo, aborta (cache en memoria quedaría stale + lock WAL)
  · sin --force NO borra altas que viven solo en la DB (delegado en reseed_with_meta,
    testeado en test_catalog_repository)
  · el backup de seguridad pre-reseed es INCONDICIONAL (tag=, no el diario) y si
    falla con una DB existente, aborta — un wipe sin red de seguridad no corre solo.
"""

import scripts.ingest_master as im
from tests.conftest import listening_socket


def test_main_aborts_when_server_alive(monkeypatch, capsys):
    s = listening_socket()
    host, port = s.getsockname()
    monkeypatch.setattr(im.settings, "host", host)
    monkeypatch.setattr(im.settings, "port", port)
    called = {"ingest": False}

    def _fake_ingest(path, allow_drop=False):
        called["ingest"] = True
        return 5

    monkeypatch.setattr(im, "ingest_from_excel", _fake_ingest)
    try:
        code = im.main(["ingest_master.py"])
    finally:
        s.close()
    assert code != 0 and called["ingest"] is False
    assert "ABORTADO" in capsys.readouterr().out


def _free_port_settings(monkeypatch):
    """Apunta settings.host/port a un puerto que se sabe libre (server apagado)."""
    s = listening_socket()
    host, port = s.getsockname()
    s.close()
    monkeypatch.setattr(im.settings, "host", host)
    monkeypatch.setattr(im.settings, "port", port)


def test_main_ingests_with_unconditional_backup_when_server_down(monkeypatch, tmp_path):
    _free_port_settings(monkeypatch)
    calls = {}

    def _fake_backup(db_path, backup_dir, **kwargs):
        calls["backup_tag"] = kwargs.get("tag")
        return tmp_path / "catalog-2026-01-02T100000-pre-reseed.db"

    def _fake_ingest(path, allow_drop=False):
        calls["drop"] = allow_drop
        return 5

    monkeypatch.setattr(im, "backup_db", _fake_backup)
    monkeypatch.setattr(im, "ingest_from_excel", _fake_ingest)

    assert im.main(["ingest_master.py"]) == 0
    assert calls["backup_tag"] == "pre-reseed"  # snapshot INCONDICIONAL, no el diario
    assert calls["drop"] is False               # sin --force no se permite perder altas

    calls.clear()
    assert im.main(["ingest_master.py", "--force"]) == 0
    assert calls["drop"] is True


def test_main_aborts_without_safety_backup(monkeypatch, tmp_path, capsys):
    """DB existente + backup de seguridad fallido → ABORTA (el wipe quedaría sin
    red); --force continúa bajo responsabilidad del operador."""
    _free_port_settings(monkeypatch)
    db = tmp_path / "catalog.db"
    db.write_bytes(b"x")  # la DB viva existe → hay algo que perder
    monkeypatch.setattr(im.settings, "catalog_db", db)
    called = {"ingest": False}

    def _fake_ingest(path, allow_drop=False):
        called["ingest"] = True
        return 5

    monkeypatch.setattr(im, "backup_db", lambda *a, **k: None)  # snapshot falló
    monkeypatch.setattr(im, "ingest_from_excel", _fake_ingest)

    code = im.main(["ingest_master.py"])
    assert code == 3 and called["ingest"] is False
    assert "backup de seguridad" in capsys.readouterr().out

    assert im.main(["ingest_master.py", "--force"]) == 0   # override consciente
    assert called["ingest"] is True


def test_main_proceeds_without_backup_on_bootstrap(monkeypatch, tmp_path):
    """Sin DB previa (bootstrap) no hay nada que perder: backup None NO aborta."""
    _free_port_settings(monkeypatch)
    monkeypatch.setattr(im.settings, "catalog_db", tmp_path / "no-existe.db")
    monkeypatch.setattr(im, "backup_db", lambda *a, **k: None)
    monkeypatch.setattr(im, "ingest_from_excel", lambda path, allow_drop=False: 7)
    assert im.main(["ingest_master.py"]) == 0
