"""Re-auditoría R5 — la perilla `MONITOR_TEST_DB_DIR` no puede sacar a la suite de %TEMP%.

La re-auditoría del lote G_tests cerró el item 3 (dos tests lanzan `pytest` adentro de
pytest y el subproceso purgaba el dir FIJO compartido) agregando una perilla nueva a
`tests/conftest.py`: `MONITOR_TEST_DB_DIR` reubica `_TEST_DB_DIR`, y cada subproceso
recibe un sandbox propio.

El problema es que esa perilla se lee del ENTORNO, o sea la misma superficie de ataque
que motivó todo el lote G (env `MONITOR_*` heredadas de la shell). Su única protección
es la condición de contención a `tempfile.gettempdir()` de `tests/conftest.py:29-32`, y
esa condición estaba SIN cubrir: comprobado por mutación —reemplazándola por
`if _TEST_DB_DIR_OVERRIDE:`— los 5 tests de tests/test_aud_G_tests_db_isolation.py y los
3 de tests/test_aud_G_tests_equivalence_guard.py siguen en VERDE.

Sin la contención, un `MONITOR_TEST_DB_DIR=%LOCALAPPDATA%\\monitor` exportado en la shell
apunta la suite ENTERA a las bases reales, y el guard `_assert_db_isolation()` no lo ve:
compara cada path contra `_TEST_DB_DIR`, que en ese caso ES el dir real. O sea que la
perilla puentea exactamente el guard que el lote G construyó, y la fixture `users` de
tests/test_auth.py (`query(UserORM).delete()`) borraría las cuentas del catálogo vivo.

El test corre `pytest` de verdad en un subproceso con un %TEMP% propio (TMPDIR/TEMP/TMP)
y la perilla apuntando FUERA de ese %TEMP%: todo queda contenido en `tmp_path`.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# El test interno que corre el subproceso: asserta que `_TEST_DB_DIR` y las 7 rutas de
# `settings` cuelgan de `tempfile.gettempdir()`.
_TEST_INTERNO = ("tests/test_aud_G_tests_db_isolation.py"
                 "::test_las_db_de_la_suite_viven_en_el_dir_temporal")
_MONITOR_ENV = ("MONITOR_CATALOG_DB", "MONITOR_PRICE_HISTORY_DB", "MONITOR_FCI_HISTORY_DB",
                "MONITOR_INDEX_HISTORY_DB", "MONITOR_RATINGS_HISTORY_DB",
                "MONITOR_BACKUP_DIR", "MONITOR_HISTORY_STATE_DIR", "MONITOR_DB_DIR")


def _correr_suite(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", _TEST_INTERNO, "-q", "-p", "no:cacheprovider"],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=600)


def _env_limpio(temp_falso) -> dict[str, str]:
    """Env sin las `MONITOR_*` heredadas y con un %TEMP% propio, para que el hijo
    resuelva `tempfile.gettempdir()` adentro de `tmp_path` y nada toque el %TEMP% real."""
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("PYTEST_ADDOPTS", None)
    for var in _MONITOR_ENV:
        env.pop(var, None)
    for var in ("TMPDIR", "TEMP", "TMP"):
        env[var] = str(temp_falso)
    return env


@pytest.mark.noauth
def test_la_perilla_de_sandbox_se_ignora_si_apunta_fuera_de_temp(tmp_path):
    """Una `MONITOR_TEST_DB_DIR` hostil NO redirige las bases: se cae al dir de %TEMP%."""
    temp_falso = tmp_path / "temp"
    temp_falso.mkdir()
    hostil = tmp_path / "monitor_real_falso"       # simula %LOCALAPPDATA%\monitor
    (hostil / "history").mkdir(parents=True)
    # Lo que habría en el db_dir REAL. Si la perilla se honrara, el conftest arranca
    # BORRÁNDOLOS: `_purge_test_dbs` se lleva todo lo que tenga ".db" en el nombre y el
    # `shutil.rmtree` posterior se lleva el dir de estado entero.
    catalogo = hostil / "catalog.db"
    catalogo.write_bytes(b"SQLite format 3\x00 catalogo vivo")
    serie = hostil / "history" / "cer_diario.csv"
    serie.write_text("estado real de las series", encoding="utf-8")

    env = _env_limpio(temp_falso)
    env["MONITOR_TEST_DB_DIR"] = str(hostil)

    proc = _correr_suite(env)
    salida = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]

    # El orden importa: la contención se chequea ANTES del returncode, porque el test
    # interno también la detecta y su mensaje ("no está en %TEMP%") tapa el diagnóstico.
    assert catalogo.exists(), (
        "`MONITOR_TEST_DB_DIR` apuntada FUERA de %TEMP% se honró y el conftest PURGÓ ese "
        "directorio: exportada en una shell apuntando al db_dir real, arrancar la suite "
        "BORRA catalog.db (fuente de verdad: las altas del ABM y los usuarios viven sólo "
        "ahí) y `_assert_db_isolation()` no lo ve, porque compara contra ese mismo dir.\n"
        + salida)
    assert serie.exists(), (
        "la perilla hostil se honró: el `shutil.rmtree` del conftest se llevó el dir de "
        "estado de las series (CER/TAMAR/A3500/reservas/BEI) del db_dir apuntado.\n"
        + salida)
    assert (temp_falso / "monitor_pytest").is_dir(), (
        "la suite no cayó al dir de bases por defecto dentro de %TEMP% (¿cambió el "
        f"nombre `monitor_pytest`?):\n{salida}")
    assert proc.returncode == 0, (
        "la suite no arrancó con una MONITOR_TEST_DB_DIR hostil (se esperaba que la "
        f"ignorara y siguiera adelante):\n{salida}")


@pytest.mark.noauth
def test_la_perilla_de_sandbox_si_se_honra_dentro_de_temp(tmp_path):
    """Control positivo: la perilla SÍ funciona cuando cae dentro de %TEMP% — es lo que
    le da a cada subproceso de pytest su propio sandbox (item 3 de la auditoría)."""
    temp_falso = tmp_path / "temp"
    temp_falso.mkdir()
    sandbox = temp_falso / "sandbox_propio"        # adentro del %TEMP% del hijo
    sandbox.mkdir()

    env = _env_limpio(temp_falso)
    env["MONITOR_TEST_DB_DIR"] = str(sandbox)

    proc = _correr_suite(env)
    salida = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]

    assert proc.returncode == 0, salida
    assert not (temp_falso / "monitor_pytest").exists(), (
        "la perilla no se honró: el subproceso usó el dir FIJO compartido, que es lo que "
        f"le borraba las .db a la sesión padre.\n{salida}")


# --------------------------------------------------------------------------- #
# El guard de aislamiento tiene que seguir a `Settings`, no a una lista copiada.
#
# El item 2 de esta re-auditoría (`history_state_dir` escribiendo en el dir REAL del
# usuario durante toda la ronda anterior) fue exactamente eso: el campo existía en
# `config.settings._DB_DERIVED` pero no en la lista hardcodeada de `_TEST_DB_ENV` ni en
# la de `_assert_db_isolation()`. Arreglar la instancia sin cerrar la clase deja el
# próximo store nuevo igual de desprotegido.
# --------------------------------------------------------------------------- #
def _fake_settings(base, extra=None):
    """Un `settings` con todos los stores dentro del sandbox, salvo los de `extra`.

    Incluye `db_dir`: el guard mira la RAÍZ además de las hojas (de `db_dir` cuelga el
    `jwt_secret`, que no es un campo de `_DB_DERIVED` y por eso ningún override por
    campo lo alcanzaba)."""
    import config.settings as cfg
    from types import SimpleNamespace

    campos = {"db_dir": base}
    campos.update({f: os.path.join(base, leaf) for f, leaf in cfg._DB_DERIVED.items()})
    campos.update(extra or {})
    return SimpleNamespace(**campos)


def test_todo_store_derivado_de_db_dir_esta_redirigido_a_temp():
    """Cada campo de `_DB_DERIVED` tiene que resolver adentro del dir de test.

    Es el chequeo dinámico que le faltó a la ronda anterior: con la lista copiada a mano,
    `history_state_dir` quedó apuntando a `%LOCALAPPDATA%\\monitor\\history` y la suite
    reescribía los CSV vivos de CER/TAMAR/A3500/reservas del usuario."""
    import config.settings as cfg
    from config.settings import settings
    from tests.conftest import _STORES_FALLBACK, _TEST_DB_DIR

    assert set(_STORES_FALLBACK) == set(cfg._DB_DERIVED), (
        "el fallback del guard (`_STORES_FALLBACK` en tests/conftest.py) quedó "
        "desincronizado de `config.settings._DB_DERIVED`: si el símbolo se renombra, el "
        "guard protege una lista vieja")
    raiz = os.path.realpath(_TEST_DB_DIR) + os.sep
    fuera = {f: str(getattr(settings, f)) for f in cfg._DB_DERIVED
             if not os.path.realpath(str(getattr(settings, f))).startswith(raiz)}
    assert not fuera, (
        "hay stores que cuelgan de `db_dir` sin redirigir en tests/conftest.py: la suite "
        f"escribe en el directorio REAL del usuario. Faltan en `_TEST_DB_ENV`: {fuera}")


def test_el_guard_de_aislamiento_cubre_un_store_nuevo_sin_editarlo(monkeypatch, tmp_path):
    """Un store agregado a `Settings` mañana tiene que ABORTAR la colección solo.

    Sin esto el guard sólo mira los 7 campos que alguien se acordó de copiar, que es la
    misma manera en que se coló el bug de `history_state_dir`."""
    import config.settings as cfg
    from tests import conftest as ct

    afuera = tmp_path / "monitor_real_falso" / "nuevo.db"     # fuera de `_TEST_DB_DIR`
    monkeypatch.setitem(cfg._DB_DERIVED, "nuevo_store_db", "nuevo.db")
    monkeypatch.setattr(
        cfg, "settings",
        _fake_settings(ct._TEST_DB_DIR, {"nuevo_store_db": str(afuera)}))

    with pytest.raises(RuntimeError, match="nuevo_store_db"):
        ct._assert_db_isolation()


def test_el_guard_no_aborta_con_todos_los_stores_adentro(monkeypatch):
    """Control positivo: el guard no es un `raise` incondicional."""
    import config.settings as cfg
    from tests import conftest as ct

    monkeypatch.setattr(cfg, "settings", _fake_settings(ct._TEST_DB_DIR))

    ct._assert_db_isolation()
