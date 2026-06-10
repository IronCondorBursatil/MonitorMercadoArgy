"""Backup recuperable de SQLite vía la API de backup ONLINE (`sqlite3.Connection.backup`).

A diferencia de copiar el archivo con shutil, la API online hace un snapshot
transaccional consistente aunque haya un write a medias o cambios pendientes en el
`-wal` — el backup nunca queda corrupto a mitad de un checkpoint.

Pensado para la `catalog.db` (fuente de verdad viva, hallazgos C1/C2): un backup por
día calendario, rotación acotada (`keep` días), y restore verificable. Los backups
viven fuera de OneDrive (junto a la .db, en %LOCALAPPDATA%\\monitor\\backups)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

logger = logging.getLogger(__name__)

_PathLike = Union[str, Path]
_PREFIX = "catalog-"
_SUFFIX = ".db"


def list_backups(backup_dir: _PathLike) -> List[Path]:
    """Backups existentes, ordenados ascendente por nombre (= cronológico, porque el
    nombre lleva fecha ISO ordenable)."""
    d = Path(backup_dir)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob(f"{_PREFIX}*{_SUFFIX}") if p.is_file())


def _online_copy(src: Path, dst: Path) -> None:
    """Snapshot consistente src→dst usando la API de backup de SQLite."""
    source = sqlite3.connect(str(src))
    try:
        dest = sqlite3.connect(str(dst))
        try:
            with dest:
                source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def backup_db(db_path: _PathLike, backup_dir: _PathLike, *, keep: int = 7,
              now: Optional[datetime] = None) -> Optional[Path]:
    """Crea un snapshot de `db_path` en `backup_dir`, a lo sumo uno por día calendario,
    y rota dejando los `keep` más recientes. Devuelve el Path creado, o None si la DB
    no existe o ya hay backup de hoy.

    `now` se inyecta en tests; en runtime usa la hora actual."""
    src = Path(db_path)
    if not src.is_file():
        return None
    stamp = (now or datetime.now()).strftime("%Y-%m-%d")
    bdir = Path(backup_dir)
    bdir.mkdir(parents=True, exist_ok=True)

    existing = list_backups(bdir)
    if any(p.name.startswith(f"{_PREFIX}{stamp}") for p in existing):
        return None  # ya hay backup de hoy — uno por día

    out = bdir / f"{_PREFIX}{stamp}{_SUFFIX}"
    try:
        _online_copy(src, out)
    except sqlite3.Error as e:
        logger.warning("backup de %s falló: %s", src.name, e)
        if out.exists():
            out.unlink(missing_ok=True)
        return None

    # Rotación: borrar los más viejos por encima de `keep`.
    for old in list_backups(bdir)[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
    logger.info("catalog backup: %s (rotación keep=%d).", out.name, keep)
    return out


def restore_db(backup_path: _PathLike, db_path: _PathLike) -> None:
    """Repone `db_path` desde un backup (snapshot online sobre la DB viva). Sobrescribe
    el contenido íntegro. Pensado para uso manual/recuperación, no en el hot-path."""
    bak = Path(backup_path)
    if not bak.is_file():
        raise FileNotFoundError(f"backup inexistente: {bak}")
    dst = Path(db_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _online_copy(bak, dst)
    logger.info("catalog restore: %s → %s.", bak.name, dst.name)
