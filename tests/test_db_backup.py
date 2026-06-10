"""Backup recuperable de la catalog.db (fuente de verdad viva, hallazgos C1/C2).

Usa la API de backup ONLINE de SQLite (snapshot consistente incluso con WAL a
medias), uno por día calendario, con rotación acotada y restore verificable."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from core.infrastructure.db.backup import backup_db, restore_db, list_backups


def _make_db(path, rows: int) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
    con.commit()
    con.close()


def _count(path) -> int:
    con = sqlite3.connect(str(path))
    try:
        return con.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        con.close()


def test_backup_creates_consistent_snapshot(tmp_path):
    db = tmp_path / "catalog.db"
    _make_db(db, 5)
    bdir = tmp_path / "backups"

    out = backup_db(db, bdir, now=datetime(2026, 1, 2, 10, 0, 0))

    assert out is not None and out.exists()
    assert out.parent == bdir
    assert _count(out) == 5  # snapshot íntegro y legible


def test_backup_missing_db_returns_none(tmp_path):
    out = backup_db(tmp_path / "nope.db", tmp_path / "backups",
                    now=datetime(2026, 1, 2, 10, 0, 0))
    assert out is None


def test_backup_at_most_one_per_day(tmp_path):
    db = tmp_path / "catalog.db"
    _make_db(db, 3)
    bdir = tmp_path / "backups"

    first = backup_db(db, bdir, now=datetime(2026, 1, 2, 10, 0, 0))
    second = backup_db(db, bdir, now=datetime(2026, 1, 2, 23, 59, 0))  # mismo día

    assert first is not None
    assert second is None, "no debe crear un segundo backup el mismo día"
    assert len(list_backups(bdir)) == 1


def test_backup_rotation_keeps_n_most_recent(tmp_path):
    db = tmp_path / "catalog.db"
    _make_db(db, 1)
    bdir = tmp_path / "backups"

    for d in (1, 2, 3, 4):
        backup_db(db, bdir, keep=2, now=datetime(2026, 1, d, 10, 0, 0))

    backups = list_backups(bdir)
    assert len(backups) == 2, "rotación debe dejar solo los 2 más recientes"
    names = sorted(p.name for p in backups)
    assert "2026-01-03" in names[0] and "2026-01-04" in names[1]


def test_restore_overwrites_live_db(tmp_path):
    db = tmp_path / "catalog.db"
    _make_db(db, 7)
    bdir = tmp_path / "backups"
    bak = backup_db(db, bdir, now=datetime(2026, 1, 2, 10, 0, 0))

    # corromper / cambiar la DB viva
    _make_db(tmp_path / "tmp.db", 0)
    db.unlink()
    _make_db(db, 1)
    assert _count(db) == 1

    restore_db(bak, db)
    assert _count(db) == 7, "restore debe reponer el contenido del backup"
