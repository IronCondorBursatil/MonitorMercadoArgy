"""F3 (review): scripts/restore_catalog.py debe negarse a restaurar con el server
vivo — la copia online sobre la DB en uso puede bloquear con 'database is locked'
y, aunque complete, el CatalogRepository sigue sirviendo el catálogo viejo desde
su cache en memoria. El guard prueba el puerto del server y aborta (salvo --force).
La detección vive en scripts/op_guards.server_running (compartida con ingest_master)."""

from __future__ import annotations

from scripts.op_guards import server_running
from scripts.restore_catalog import main
from tests.conftest import listening_socket


def test_server_running_detects_listener():
    s = listening_socket()
    try:
        host, port = s.getsockname()
        assert server_running(host, port) is True
    finally:
        s.close()


def test_server_running_false_on_closed_port():
    s = listening_socket()
    host, port = s.getsockname()
    s.close()   # puerto liberado
    assert server_running(host, port) is False


def test_main_aborts_restore_when_server_alive(monkeypatch, capsys):
    """Con el server vivo, main() con un target debe abortar SIN llamar restore_db."""
    import scripts.restore_catalog as rc

    s = listening_socket()
    host, port = s.getsockname()
    monkeypatch.setattr(rc.settings, "host", host)
    monkeypatch.setattr(rc.settings, "port", port)

    called = {"restore": False}
    monkeypatch.setattr(rc, "restore_db", lambda *a, **k: called.__setitem__("restore", True))
    monkeypatch.setattr(rc, "list_backups", lambda d: [type("P", (), {
        "name": "catalog-2026-01-01.db", "stat": lambda self: type("S", (), {"st_size": 1024})()})()])

    try:
        rc_code = main(["restore_catalog.py", "--latest"])
    finally:
        s.close()

    assert rc_code != 0, "debe abortar con exit code != 0"
    assert called["restore"] is False, "NO debe restaurar con el server vivo"
    out = capsys.readouterr().out.lower()
    assert "server" in out or "corriendo" in out, "debe explicar por qué abortó"


def _free_port_settings(monkeypatch, rc):
    s = listening_socket()
    host, port = s.getsockname()
    s.close()
    monkeypatch.setattr(rc.settings, "host", host)
    monkeypatch.setattr(rc.settings, "port", port)


def test_main_force_bypasses_guard(monkeypatch, tmp_path):
    """--force saltea el guard (uso consciente) — incluso sin backup de seguridad."""
    import scripts.restore_catalog as rc

    s = listening_socket()
    host, port = s.getsockname()
    monkeypatch.setattr(rc.settings, "host", host)
    monkeypatch.setattr(rc.settings, "port", port)

    bak = tmp_path / "catalog-2026-01-01.db"
    bak.write_bytes(b"")
    called = {"restore": False}
    monkeypatch.setattr(rc, "restore_db", lambda *a, **k: called.__setitem__("restore", True))
    monkeypatch.setattr(rc, "backup_db", lambda *a, **k: None)
    monkeypatch.setattr(rc, "list_backups", lambda d: [bak])

    try:
        code = main(["restore_catalog.py", "--latest", "--force"])
    finally:
        s.close()

    assert code == 0
    assert called["restore"] is True


def test_main_aborts_without_safety_backup(monkeypatch, tmp_path, capsys):
    """DB viva existente + backup de seguridad fallido → ABORTA: el restore pisa
    el estado actual entero y sin snapshot no hay vuelta atrás."""
    import scripts.restore_catalog as rc

    _free_port_settings(monkeypatch, rc)
    db = tmp_path / "catalog.db"
    db.write_bytes(b"x")
    monkeypatch.setattr(rc.settings, "catalog_db", db)

    bak = tmp_path / "catalog-2026-01-01.db"
    bak.write_bytes(b"")
    called = {"restore": False}
    monkeypatch.setattr(rc, "restore_db", lambda *a, **k: called.__setitem__("restore", True))
    monkeypatch.setattr(rc, "backup_db", lambda *a, **k: None)   # snapshot falló
    monkeypatch.setattr(rc, "list_backups", lambda d: [bak])

    code = main(["restore_catalog.py", "--latest"])
    assert code == 3 and called["restore"] is False
    assert "backup de seguridad" in capsys.readouterr().out
