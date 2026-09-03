"""Auditoría G_tests — la suite NUNCA puede tocar las .db reales del usuario.

`tests/conftest.py` aísla las bases con `os.environ.setdefault(...)`: si el proceso
que lanza pytest ya trae `MONITOR_CATALOG_DB` (una shell donde se exportó para
apuntar la app a otra base, el `EnvironmentFile` del droplet con dev-deps
instaladas), el setdefault es un no-op y los tests escriben sobre ESA base — la
fixture `users` de tests/test_auth.py hace `s.query(UserORM).delete()`, o sea que
borra las cuentas reales, y test_catalog_router deja tickers ficticios en el
catálogo VIVO (que es la fuente de verdad, ver CLAUDE.md).

También cubre el dir de test persistente: sin purga, la catalog.db de pytest queda
congelada en el `instruments_master.xlsx` del día en que esa máquina corrió la
suite por primera vez (CatalogRepository sólo siembra si la tabla está VACÍA).
"""

import os
import subprocess
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HOSTILE = ("MONITOR_CATALOG_DB", "MONITOR_PRICE_HISTORY_DB", "MONITOR_FCI_HISTORY_DB",
            "MONITOR_INDEX_HISTORY_DB", "MONITOR_RATINGS_HISTORY_DB", "MONITOR_BACKUP_DIR",
            "MONITOR_HISTORY_STATE_DIR")


@pytest.mark.noauth
def test_las_db_de_la_suite_viven_en_el_dir_temporal():
    """Invariante: toda base que la suite abra por default está en %TEMP%.

    Corre también como test "interno" del subproceso de abajo, con env hostil."""
    from config.settings import settings
    from tests.conftest import _TEST_DB_DIR

    tmp = os.path.realpath(tempfile.gettempdir())
    test_dir = os.path.realpath(_TEST_DB_DIR)
    assert test_dir.startswith(tmp + os.sep), (
        f"el dir de bases de la suite ({test_dir}) no está en %TEMP%")
    paths = {
        "catalog_db": settings.catalog_db,
        "price_history_db": settings.price_history_db,
        "fci_history_db": settings.fci_history_db,
        "index_history_db": settings.index_history_db,
        "ratings_history_db": settings.ratings_history_db,
        "backup_dir": settings.backup_dir,
        # Estado de runtime de las series (CER/TAMAR/A3500/reservas/BEI/CAFCI): sin
        # redirigirlo, la suite REESCRIBE los CSV vivos de `%LOCALAPPDATA%\monitor\history`.
        "history_state_dir": settings.history_state_dir,
    }
    fuera = {k: str(v) for k, v in paths.items()
             if not os.path.realpath(str(v)).startswith(test_dir + os.sep)}
    assert not fuera, (
        "la suite apunta a bases FUERA del dir de test (puede borrar usuarios y "
        f"ensuciar el catálogo vivo, que es la fuente de verdad): {fuera}")


@pytest.mark.noauth
def test_la_suite_ignora_las_env_monitor_heredadas(tmp_path):
    """Con `MONITOR_*` heredadas del entorno, la suite igual usa las .db de test."""
    prod = tmp_path / "prod"
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("PYTEST_ADDOPTS", None)
    # Sandbox propio: el subproceso re-importa el conftest y vuelve a purgar. Con el dir
    # FIJO compartido le borraría las .db a la sesión PADRE, que las tiene abiertas (en
    # Windows el lock del SO lo tapa; en POSIX el unlink procede).
    env["MONITOR_TEST_DB_DIR"] = str(sandbox)
    for var in _HOSTILE:
        env[var] = str(prod / (var.lower().replace("monitor_", "") + ".db"))
    env["MONITOR_BACKUP_DIR"] = str(prod / "backups")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_aud_G_tests_db_isolation.py::test_las_db_de_la_suite_viven_en_el_dir_temporal",
         "-q", "-p", "no:cacheprovider"],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    out = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
    assert proc.returncode == 0, (
        "las MONITOR_* heredadas del entorno redirigen las bases de la SUITE a la DB "
        "real: un `pytest tests/` en esa shell borra los usuarios del catálogo vivo.\n"
        + out)
    assert not prod.exists(), f"la suite creó archivos en la base heredada: {prod}"


def test_la_purga_borra_las_db_de_test_viejas(tmp_path):
    """La catalog.db de pytest no puede sobrevivir entre corridas: se siembra del
    Excel SOLO si está vacía, así que una base vieja congela el universo de
    instrumentos (y acumula las filas que dejan los tests)."""
    from tests.conftest import _purge_test_dbs

    db = tmp_path / "catalog.db"
    db.write_bytes(b"vieja")
    (tmp_path / "catalog.db-wal").write_bytes(b"wal")
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "catalog-2020.db").write_bytes(b"backup")

    _purge_test_dbs(tmp_path)

    assert not db.exists()
    assert not (tmp_path / "catalog.db-wal").exists()
    assert tmp_path.exists(), "la purga no debe borrar el dir, solo las bases"


def test_la_purga_tolera_una_db_en_uso(tmp_path):
    """Dos corridas simultáneas comparten el dir: si la base está tomada, la purga
    la saltea en silencio en vez de reventar la colección."""
    from tests.conftest import _purge_test_dbs

    db = tmp_path / "catalog.db"
    db.write_bytes(b"en uso")
    fh = open(db, "rb")
    try:
        _purge_test_dbs(tmp_path)      # no debe propagar PermissionError
    finally:
        fh.close()


@pytest.mark.noauth
def test_la_suite_purga_de_verdad_su_dir_al_arrancar(tmp_path):
    """El invariante del hallazgo 5 es la INVOCACIÓN, no el helper.

    Los dos tests de arriba ejercitan `_purge_test_dbs` contra un `tmp_path`: pasan
    igual si alguien borra la línea `_purge_test_dbs(_TEST_DB_DIR)` de
    `tests/conftest.py` (comprobado por el auditor) y el bug vuelve entero — la
    `catalog.db` de pytest sobrevive entre corridas y congela el universo de
    instrumentos en el `instruments_master.xlsx` del día en que esa máquina corrió la
    suite por primera vez.

    Acá se planta un marcador `.db` en un sandbox propio (`MONITOR_TEST_DB_DIR`) y se
    corre `pytest` de verdad adentro: si el conftest no purga, el marcador sobrevive.
    """
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    marcador = sandbox / "zz_marker.db"
    marcador.write_bytes(b"base de la corrida anterior")
    (sandbox / "catalog.db-wal").write_bytes(b"wal viejo")
    ajeno = sandbox / "readme.txt"          # la purga sólo se lleva las bases
    ajeno.write_bytes(b"no soy una base")
    # Estado de las series de la corrida anterior: `resolve_read` prefiere el estado a
    # la semilla versionada, así que un CSV que sobreviva vuelve la suite dependiente
    # de lo que dejó la corrida de ayer en vez de leer `data/history/`.
    (sandbox / "history").mkdir()
    viejo_csv = sandbox / "history" / "cer_diario.csv"
    viejo_csv.write_text("estado de las series de la corrida anterior", encoding="utf-8")

    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("PYTEST_ADDOPTS", None)
    for var in _HOSTILE:
        env.pop(var, None)
    env["MONITOR_TEST_DB_DIR"] = str(sandbox)

    proc = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_aud_G_tests_db_isolation.py::test_las_db_de_la_suite_viven_en_el_dir_temporal",
         "-q", "-p", "no:cacheprovider"],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    out = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
    assert proc.returncode == 0, f"la corrida de control falló por otra cosa:\n{out}"
    assert not marcador.exists(), (
        "el conftest NO purga su dir de bases al arrancar: la catalog.db de pytest "
        "sobrevive entre corridas y congela el universo de instrumentos (se siembra "
        f"del Excel SOLO si la tabla está vacía). Marcador vivo: {marcador}\n{out}")
    assert not (sandbox / "catalog.db-wal").exists(), (
        "la purga dejó el -wal de la corrida anterior")
    assert ajeno.exists(), "la purga se llevó un archivo que no es una base"
    assert not viejo_csv.exists(), (
        "el conftest no limpia el estado de las series: los CSV de la corrida anterior "
        "le ganan a la semilla versionada (`data/history/`) en el próximo arranque")
    assert sandbox.is_dir(), "la purga borró el dir, no sólo las bases"
