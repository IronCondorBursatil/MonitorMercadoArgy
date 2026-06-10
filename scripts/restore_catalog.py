"""Restore manual de la catalog.db desde un backup.

Uso:
    py -3.12 scripts/restore_catalog.py            # lista backups disponibles
    py -3.12 scripts/restore_catalog.py <archivo>  # restaura ese backup
    py -3.12 scripts/restore_catalog.py --latest   # restaura el más reciente
    ... --force                                    # saltea el guard de server vivo

PARÁ EL SERVER PRIMERO: restaurar sobre la DB en uso puede bloquear contra las
conexiones WAL del pool ('database is locked') y, aunque complete, el server sigue
sirviendo el catálogo VIEJO desde su cache en memoria hasta reiniciar. El script
prueba el puerto del monitor y aborta si responde (override consciente: --force).

Los backups los genera el arranque del server (1×/día, ver backup.py). La DB viva
se sobrescribe íntegra; se hace un backup de seguridad del estado actual antes.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from core.infrastructure.db.backup import backup_db, list_backups, restore_db  # noqa: E402


def _server_running(host: str, port: int, timeout_s: float = 1.0) -> bool:
    """True si hay algo escuchando en host:port (el monitor vivo)."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _print_backups(backups) -> None:
    if not backups:
        print(f"No hay backups en {settings.backup_dir}")
        return
    print(f"Backups en {settings.backup_dir}:")
    for i, p in enumerate(backups):
        size_kb = p.stat().st_size / 1024
        print(f"  [{i}] {p.name}  ({size_kb:,.0f} KB)")


def main(argv: list[str]) -> int:
    force = "--force" in argv
    args = [a for a in argv[1:] if a != "--force"]
    backups = list_backups(settings.backup_dir)
    arg = args[0] if args else None

    if arg is None:
        _print_backups(backups)
        print("\nPasá un nombre de archivo o --latest para restaurar.")
        return 0

    # Guard F3: nunca restaurar sobre la DB de un server vivo (lock WAL + cache
    # en memoria desactualizado). --force lo saltea conscientemente.
    if not force and _server_running(settings.host, settings.port):
        print(f"ABORTADO: el monitor está corriendo en {settings.host}:{settings.port}.")
        print("Pará el server primero (la DB en uso puede bloquear el restore y el")
        print("catálogo en memoria quedaría desactualizado). Para forzar: --force")
        return 2

    if not backups:
        print("No hay backups para restaurar.")
        return 1

    target = backups[-1] if arg == "--latest" else next(
        (p for p in backups if p.name == arg or str(p) == arg), None)
    if target is None:
        print(f"Backup no encontrado: {arg}")
        _print_backups(backups)
        return 1

    print(f"Restaurando {target.name} → {settings.catalog_db}")
    # Red de seguridad: snapshot del estado ACTUAL antes de sobrescribir.
    safety = backup_db(settings.catalog_db, settings.backup_dir, keep=settings.backup_keep)
    if safety:
        print(f"  (backup de seguridad del estado actual: {safety.name})")
    restore_db(target, settings.catalog_db)
    print("Restore completo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
