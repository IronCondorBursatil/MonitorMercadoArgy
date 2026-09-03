"""Auditoría F_ops — `config/settings.py`: dónde caen las bases `.db`.

Dos hallazgos, un solo mecanismo:

  · **`MONITOR_DB_DIR` era una perilla MUERTA.** Los 7 paths derivados
    (`catalog_db`, `backup_dir`, `history_state_dir`, `price_history_db`,
    `fci_history_db`, `ratings_history_db`, `index_history_db`) se evaluaban en el
    CUERPO de la clase contra el default estático, así que mover `db_dir` no movía
    ninguna base: había que enumerar 7 env vars — y la lista crece con cada store
    nuevo (`history_state_dir` y `ratings_history_db` se agregaron después y nadie
    actualizó la receta).

  · **El default en Linux caía DENTRO del working tree.** `LOCALAPPDATA` es una
    variable de Windows: sin ella, `db_dir` era `<repo>/monitor`, o sea la
    `catalog.db` (FUENTE DE VERDAD, con las altas ABM que viven sólo ahí), los
    backups y el `jwt_secret` quedaban en el árbol donde `deploy.sh` corre
    `git pull` — un `git clean -xfd` se los lleva a todos juntos.

Más el hallazgo 8 (nadie crea el directorio padre de las `.db` cuando el operador
sigue la receta de CLAUDE.md y las apunta por campo fuera del árbol).
"""

from __future__ import annotations

import logging

import pytest

import config.settings as settings_mod
from config.settings import Settings

_DB_ENV = (
    "MONITOR_DB_DIR",
    "MONITOR_CATALOG_DB",
    "MONITOR_BACKUP_DIR",
    "MONITOR_HISTORY_STATE_DIR",
    "MONITOR_PRICE_HISTORY_DB",
    "MONITOR_FCI_HISTORY_DB",
    "MONITOR_RATINGS_HISTORY_DB",
    "MONITOR_INDEX_HISTORY_DB",
)


@pytest.fixture
def clean_db_env(monkeypatch):
    """Saca del entorno los overrides por campo que pone `tests/conftest.py`, para
    poder ejercer los DEFAULTS y la derivación desde `db_dir`."""
    for var in _DB_ENV:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _derived(s: Settings):
    return (s.catalog_db, s.backup_dir, s.history_state_dir, s.price_history_db,
            s.fci_history_db, s.ratings_history_db, s.index_history_db)


def test_monitor_db_dir_reubica_todas_las_bases(clean_db_env, tmp_path):
    """La perilla documentada tiene que mover las 7 rutas, no sólo `db_dir`."""
    destino = tmp_path / "fuera-del-arbol"
    clean_db_env.setenv("MONITOR_DB_DIR", str(destino))

    s = Settings()

    assert s.db_dir == destino
    for p in _derived(s):
        assert p.parent == destino, f"{p} no siguió a db_dir"


def test_override_por_campo_sigue_ganando(clean_db_env, tmp_path):
    """La derivación no puede pisar el override explícito por campo (es la receta
    que CLAUDE.md le da al operador de prod)."""
    clean_db_env.setenv("MONITOR_DB_DIR", str(tmp_path / "generico"))
    clean_db_env.setenv("MONITOR_CATALOG_DB", str(tmp_path / "especifico" / "cat.db"))

    s = Settings()

    assert s.catalog_db == tmp_path / "especifico" / "cat.db"
    assert s.price_history_db.parent == tmp_path / "generico"


def test_default_sin_localappdata_no_cae_en_el_working_tree(clean_db_env, tmp_path):
    """Linux (el droplet): sin `LOCALAPPDATA` el default NO puede ser `<repo>/monitor`."""
    clean_db_env.delenv("LOCALAPPDATA", raising=False)
    clean_db_env.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    s = Settings()

    assert not s.db_dir.is_relative_to(s.base_dir), (
        f"db_dir={s.db_dir} quedó dentro del working tree {s.base_dir}")
    assert s.catalog_db == tmp_path / "xdg" / "monitor" / "catalog.db"


def test_avisa_fuerte_si_una_base_cae_dentro_del_working_tree(clean_db_env, tmp_path,
                                                              caplog):
    """Guard: si igual quedan adentro (droplet ya desplegado, override a mano), tiene
    que salir un ERROR visible nombrando el path — no un fallback mudo."""
    with caplog.at_level(logging.ERROR, logger=settings_mod.__name__):
        Settings(base_dir=tmp_path, db_dir=tmp_path / "monitor")

    texto = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert "catalog.db" in texto
    assert "git clean" in texto.lower()


def test_crea_el_directorio_padre_de_cada_base(clean_db_env, tmp_path):
    """Hallazgo 8: seguir la receta de CLAUDE.md (paths por campo fuera del árbol)
    apuntando a un directorio que todavía no existe reventaba con
    `sqlite3.OperationalError: unable to open database file`."""
    destino = tmp_path / "var" / "lib" / "monitor"
    clean_db_env.setenv("MONITOR_CATALOG_DB", str(destino / "catalog.db"))
    clean_db_env.setenv("MONITOR_RATINGS_HISTORY_DB",
                        str(tmp_path / "otro" / "ratings_history.db"))

    Settings()

    assert destino.is_dir()
    assert (tmp_path / "otro").is_dir()
