"""Engine SQLite + sessionmaker. WAL para lectores concurrentes y resistencia
a escrituras concurrentes (lectores no bloquean al escritor).

El engine es **reconfigurable** (`configure(db_path)`): los tests apuntan a una
.db temporal sin tocar la catalog.db real. `SessionLocal` es un sessionmaker
único que se re-bindea in-place, así los módulos que ya lo importaron siguen
viendo el engine vigente.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from config.settings import settings

# Sessionmaker único (sin bind hasta configure()); se re-bindea in-place.
SessionLocal = sessionmaker(future=True, expire_on_commit=False)

_ENGINE: Optional[Engine] = None


def _pragmas(conn, _record):
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")   # lectores concurrentes
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def configure(db_path: Union[str, Path]) -> Engine:
    """(Re)crea el engine apuntando a `db_path` y re-bindea SessionLocal.

    Idempotente por path; dispone el engine anterior si lo había."""
    global _ENGINE
    if _ENGINE is not None:
        _ENGINE.dispose()
    # as_posix(): en Windows la URL sqlite necesita forward-slashes.
    _ENGINE = create_engine(f"sqlite:///{Path(db_path).as_posix()}", future=True)
    event.listen(_ENGINE, "connect", _pragmas)
    SessionLocal.configure(bind=_ENGINE)
    return _ENGINE


def get_engine() -> Engine:
    if _ENGINE is None:
        configure(settings.catalog_db)
    return _ENGINE


# Bind inicial a la catalog.db de settings.
configure(settings.catalog_db)
