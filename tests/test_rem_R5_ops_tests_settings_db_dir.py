"""Re-auditoría R5 — las dos ramas de `config/settings.py` que deciden si arranca el droplet.

La re-auditoría del lote F_ops marcó que «las dos decisiones más riesgosas del lote
quedaron SIN test»:

  · **La adopción del layout legacy** en `_default_db_dir()`: en POSIX el default se
    mudó a `$XDG_DATA_HOME/monitor` (fuera del working tree), PERO si ya existe un
    `<repo>/monitor/catalog.db` se lo respeta. Si esa rama se pierde, el droplet ya
    desplegado arranca contra una base VACÍA —sin las altas del ABM ni los usuarios,
    que viven SOLO ahí (CLAUDE.md)— y deja la base viva huérfana; y si se invierte
    (adoptar siempre el legacy), el fix del hallazgo se deshace y las bases vuelven
    al árbol donde `deploy.sh` corre `git pull` / `git clean -xfd`.

  · **`db_in_tree_fatal`**: la perilla `MONITOR_DB_IN_TREE_FATAL=true` que la nota de
    deploy recomienda agregar a `monitores.service` una vez migradas las bases. Con
    ella el invariante "nada de .db adentro del proyecto" pasa de ERROR logueado a
    `RuntimeError` que ABORTA el arranque.

Estos tests NO modifican `config/settings.py`: lo ejercitan desde afuera, apuntando
`_BASE_DIR`/`base_dir` a un `tmp_path` para simular el repo del droplet.
"""

from __future__ import annotations

import logging

import pytest

import config.settings as settings_mod
from config.settings import Settings

pytestmark = pytest.mark.noauth

_DB_ENV = (
    "MONITOR_DB_DIR",
    "MONITOR_CATALOG_DB",
    "MONITOR_BACKUP_DIR",
    "MONITOR_HISTORY_STATE_DIR",
    "MONITOR_PRICE_HISTORY_DB",
    "MONITOR_FCI_HISTORY_DB",
    "MONITOR_RATINGS_HISTORY_DB",
    "MONITOR_INDEX_HISTORY_DB",
    "MONITOR_DB_IN_TREE_FATAL",
)


@pytest.fixture
def droplet(monkeypatch, tmp_path):
    """Simula el droplet: sin `LOCALAPPDATA` (es de Windows), sin los overrides por
    campo que pone tests/conftest.py, con un `XDG_DATA_HOME` propio y `_BASE_DIR`
    apuntando a un repo de mentira. Devuelve la raíz de ese repo."""
    for var in _DB_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "repo"
    (repo / "monitor").mkdir(parents=True)
    monkeypatch.setattr(settings_mod, "_BASE_DIR", repo)
    return repo


def _sembrar_catalogo_legacy(repo):
    """El `<repo>/monitor/catalog.db` de un droplet desplegado antes del fix."""
    db = repo / "monitor" / "catalog.db"
    db.write_bytes(b"SQLite format 3\x00")
    return db


# --------------------------------------------------------------------------- #
# Rama 1: adopción del layout legacy
# --------------------------------------------------------------------------- #
def test_adopta_el_monitor_del_repo_si_ya_hay_una_catalog_db_viva(droplet):
    """Un droplet ya desplegado NO puede mudarse solo: perdería el catálogo vivo."""
    _sembrar_catalogo_legacy(droplet)

    assert settings_mod._default_db_dir() == droplet / "monitor", (
        "el default se mudó aunque ya hay un <repo>/monitor/catalog.db: el droplet "
        "arrancaría con un catálogo VACÍO (las altas del ABM y los usuarios viven "
        "sólo en esa base) y la base viva quedaría huérfana")


def test_sin_catalog_db_legacy_el_default_sale_del_working_tree(droplet):
    """Sin base legacy manda XDG: el fix del hallazgo (bases fuera del árbol)."""
    assert settings_mod._default_db_dir() == droplet.parent / "xdg" / "monitor", (
        "sin una base legacy el default tiene que caer FUERA del working tree "
        "(donde deploy.sh corre git pull y un git clean -xfd se lo lleva todo)")


def test_un_directorio_monitor_vacio_no_dispara_la_adopcion(droplet):
    """La condición es la BASE, no el directorio: un `monitor/` con restos (backups,
    un jwt_secret suelto) pero sin `catalog.db` no justifica quedarse en el árbol."""
    (droplet / "monitor" / "backups").mkdir()
    (droplet / "monitor" / "jwt_secret").write_text("x", encoding="utf-8")

    assert settings_mod._default_db_dir() == droplet.parent / "xdg" / "monitor"


def test_la_adopcion_legacy_llega_hasta_settings_y_se_denuncia(droplet, caplog):
    """De punta a punta: `Settings()` adopta el legacy Y el guard lo grita.

    Es la combinación exacta del droplet de hoy: sigue sirviendo (no se le mueve la
    base abajo) pero el operador ve el ERROR con el path en cada arranque."""
    _sembrar_catalogo_legacy(droplet)

    with caplog.at_level(logging.ERROR, logger=settings_mod.__name__):
        s = Settings(base_dir=droplet)

    assert s.db_dir == droplet / "monitor"
    assert s.catalog_db == droplet / "monitor" / "catalog.db"
    texto = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert "catalog.db" in texto, f"la adopción legacy no se denunció: {texto!r}"
    assert "git clean" in texto.lower()


# --------------------------------------------------------------------------- #
# Rama 2: db_in_tree_fatal
# --------------------------------------------------------------------------- #
def test_db_in_tree_fatal_aborta_el_arranque(droplet):
    """Con la perilla dura, una base adentro del árbol NO deja arrancar."""
    with pytest.raises(RuntimeError) as exc:
        Settings(base_dir=droplet, db_dir=droplet / "monitor", db_in_tree_fatal=True)

    msg = str(exc.value)
    assert "catalog.db" in msg, f"el error no nombra la base ofensora: {msg}"
    assert "git clean" in msg.lower()
    assert "MONITOR_DB_DIR" in msg, "el error no dice cómo salir del paso"


def test_db_in_tree_fatal_se_prende_por_env(droplet):
    """`MONITOR_DB_IN_TREE_FATAL=true` es la perilla que va al EnvironmentFile de
    `monitores.service` una vez migradas las bases (nota de deploy de la auditoría)."""
    import os

    os.environ["MONITOR_DB_IN_TREE_FATAL"] = "true"
    try:
        with pytest.raises(RuntimeError):
            Settings(base_dir=droplet, db_dir=droplet / "monitor")
    finally:
        os.environ.pop("MONITOR_DB_IN_TREE_FATAL", None)


def test_db_in_tree_fatal_no_molesta_con_las_bases_afuera(droplet, tmp_path):
    """La perilla dura sólo dispara con bases DENTRO del árbol: con el layout
    recomendado (/var/lib/monitor) el arranque es normal."""
    afuera = tmp_path / "var-lib-monitor"

    s = Settings(base_dir=droplet, db_dir=afuera, db_in_tree_fatal=True)

    assert s.catalog_db == afuera / "catalog.db"
    assert s.history_state_dir == afuera / "history"


def test_sin_la_perilla_solo_se_loguea_y_la_app_arranca(droplet, caplog):
    """El default sigue siendo NO fatal: un droplet desplegado antes del fix tiene
    que poder arrancar (con el ERROR a la vista), no quedarse sin servicio."""
    with caplog.at_level(logging.ERROR, logger=settings_mod.__name__):
        s = Settings(base_dir=droplet, db_dir=droplet / "monitor")

    assert s.db_in_tree_fatal is False
    assert s.catalog_db == droplet / "monitor" / "catalog.db"
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "una base dentro del árbol pasó en SILENCIO: el operador no tiene ninguna señal")
