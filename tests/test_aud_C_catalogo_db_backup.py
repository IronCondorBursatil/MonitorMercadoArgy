"""Auditoría lote C — hallazgo #5: el dedup del backup diario mira TODO el pool.

`backup_db` deduplica el snapshot diario con `p.name.startswith("catalog-<fecha>")`
sobre TODOS los backups, y ese prefijo también matchea los tagged
(`catalog-YYYY-MM-DDThhmmss-<tag>.db`). La ROTACIÓN sí separa los pools ("T" en el
stem); el dedup es la única pieza que los mezcla, contra lo que declaran el
docstring del módulo y `config/settings.py`.

Consecuencia: si en el día D corre primero un script con snapshot pre-op
(`op_guards.guard_write` → todos los `ingest_*`/`pin_*`/`restore_catalog`), el
backup DIARIO de D ya no se crea nunca — y el tagged es, por definición, el estado
PREVIO a esa operación. El estado post-op de ese día no queda en ninguna serie
durable hasta el diario de D+1.
"""

import sqlite3
from datetime import datetime

from core.infrastructure.db.backup import backup_db, list_backups


def _db(tmp_path):
    p = tmp_path / "catalog.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.commit()
    con.close()
    return p


def test_el_diario_se_crea_aunque_ya_haya_un_tagged_del_mismo_dia(tmp_path):
    db, bdir = _db(tmp_path), tmp_path / "backups"
    pre = backup_db(db, bdir, tag="pre-on-iamc", now=datetime(2026, 9, 3, 10, 0, 0))
    assert pre is not None and "pre-on-iamc" in pre.name

    daily = backup_db(db, bdir, now=datetime(2026, 9, 3, 10, 30, 0))
    assert daily is not None, "el tagged bloqueó el diario"
    assert daily.name == "catalog-2026-09-03.db"


def test_el_diario_sigue_siendo_uno_por_dia(tmp_path):
    """No-regresión: dos llamadas sin tag el mismo día → una sola copia."""
    db, bdir = _db(tmp_path), tmp_path / "backups"
    assert backup_db(db, bdir, now=datetime(2026, 9, 3, 0, 5, 0)) is not None
    assert backup_db(db, bdir, now=datetime(2026, 9, 3, 12, 0, 0)) is None
    assert backup_db(db, bdir, now=datetime(2026, 9, 3, 23, 59, 0)) is None
    assert [p.name for p in list_backups(bdir)] == ["catalog-2026-09-03.db"]


def test_los_dos_pools_conviven_el_mismo_dia(tmp_path):
    db, bdir = _db(tmp_path), tmp_path / "backups"
    backup_db(db, bdir, tag="pre-reseed", now=datetime(2026, 9, 3, 10, 0, 0))
    backup_db(db, bdir, now=datetime(2026, 9, 3, 10, 30, 0))
    backup_db(db, bdir, tag="pre-restore", now=datetime(2026, 9, 3, 18, 0, 0))
    names = sorted(p.name for p in list_backups(bdir))
    assert names == ["catalog-2026-09-03.db",
                     "catalog-2026-09-03T100000-pre-reseed.db",
                     "catalog-2026-09-03T180000-pre-restore.db"]
