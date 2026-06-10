"""F3 (review): scripts/restore_catalog.py debe negarse a restaurar con el server
vivo — la copia online sobre la DB en uso puede bloquear con 'database is locked'
y, aunque complete, el CatalogRepository sigue sirviendo el catálogo viejo desde
su cache en memoria. El guard prueba el puerto del server y aborta (salvo --force)."""

from __future__ import annotations

import socket
import threading

from scripts.restore_catalog import _server_running, main


def _listening_socket():
    """Socket TCP escuchando en un puerto efímero de localhost."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s


def test_server_running_detects_listener():
    s = _listening_socket()
    try:
        host, port = s.getsockname()
        assert _server_running(host, port) is True
    finally:
        s.close()


def test_server_running_false_on_closed_port():
    s = _listening_socket()
    host, port = s.getsockname()
    s.close()   # puerto liberado
    assert _server_running(host, port) is False


def test_main_aborts_restore_when_server_alive(monkeypatch, capsys):
    """Con el server vivo, main() con un target debe abortar SIN llamar restore_db."""
    import scripts.restore_catalog as rc

    s = _listening_socket()
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


def test_main_force_bypasses_guard(monkeypatch, tmp_path):
    """--force saltea el guard (uso consciente)."""
    import scripts.restore_catalog as rc

    s = _listening_socket()
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
