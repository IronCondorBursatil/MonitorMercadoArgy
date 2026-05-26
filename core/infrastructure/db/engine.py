"""Engine SQLite + sessionmaker. WAL para lectores concurrentes y resistencia
al sync de OneDrive (aunque la .db vive fuera de OneDrive, WAL no estorba)."""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from config.settings import settings

# as_posix(): en Windows la URL sqlite necesita forward-slashes.
_ENGINE = create_engine(f"sqlite:///{settings.catalog_db.as_posix()}", future=True)


@event.listens_for(_ENGINE, "connect")
def _pragmas(conn, _record):
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")   # lectores concurrentes
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=_ENGINE, future=True, expire_on_commit=False)


def get_engine():
    return _ENGINE
