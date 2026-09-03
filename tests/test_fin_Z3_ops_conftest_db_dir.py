"""Cierre Z3 (ítem 5) — `MONITOR_DB_DIR` faltaba en el aislamiento de la suite.

`tests/conftest.py` redirige a `%TEMP%\\monitor_pytest` las 7 rutas que `Settings`
deriva de `db_dir` (`_DB_DERIVED`: catalog / backups / history / price / fci / ratings /
index), pero **no `db_dir` en sí**. Verificado en vivo antes del arreglo::

    db_dir     = C:\\Users\\david\\AppData\\Local\\monitor      <-- el REAL del usuario
    catalog_db = C:\\Users\\david\\AppData\\Local\\Temp\\monitor_pytest\\catalog.db

O sea que durante toda la suite `settings.db_dir` era el directorio de producción. El
override por campo tapaba las hojas, pero `db_dir` tiene un consumidor directo que
ningún override por campo alcanza: **el `jwt_secret`**. `settings.model_post_init` llama
a `_resolve_jwt_secret(self.db_dir)`, que LEE `db_dir/jwt_secret` y, si no existe, lo
**genera y lo persiste con 0600**. Consecuencias:

  · la suite firmaba/validaba sus JWT con el secreto de PRODUCCIÓN (el mismo que las
    sesiones vivas del usuario), en vez de con uno desechable;
  · en una máquina o CI donde ese archivo todavía no exista —droplet recién provisto,
    runner limpio—, correr los tests lo **crea dentro del db_dir real**, y la app lo
    adopta después: el secreto de producción pasa a ser uno nacido en un test;
  · y el guard `_assert_db_isolation()`, cuyo contrato es abortar la colección si algo
    resuelve fuera del sandbox, no lo veía: sólo miraba las hojas de `_DB_DERIVED`.

El arreglo es una línea en `_TEST_DB_ENV` (`MONITOR_DB_DIR`) + `db_dir` incorporado al
guard, para que la RAÍZ quede cubierta y no dependa de que alguien se acuerde de
redirigir el próximo store que cuelgue de ella.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import config.settings as cfg
from config.settings import settings
from tests.conftest import _TEST_DB_DIR, _assert_db_isolation

pytestmark = pytest.mark.noauth


def _adentro_del_sandbox(path) -> bool:
    raiz = os.path.realpath(_TEST_DB_DIR)
    real = os.path.realpath(str(path))
    return real == raiz or real.startswith(raiz + os.sep)


def test_db_dir_resuelve_dentro_del_sandbox():
    """La raíz, no sólo las hojas."""
    assert _adentro_del_sandbox(settings.db_dir), (
        f"settings.db_dir = {settings.db_dir} — la suite está apuntando al directorio "
        f"REAL del usuario en vez de {_TEST_DB_DIR}. Falta MONITOR_DB_DIR en "
        "`_TEST_DB_ENV` (tests/conftest.py)")


def test_el_jwt_secret_no_sale_del_sandbox():
    """El consumidor concreto de `db_dir` que ningún override por campo cubre.

    No se mira sólo el path: se comprueba que el archivo REAL del usuario no sea el que
    la suite está usando (si existiera y se leyera, los JWT de los tests se firmarían
    con el secreto de producción)."""
    secreto_de_la_suite = Path(settings.db_dir) / "jwt_secret"
    assert _adentro_del_sandbox(secreto_de_la_suite), (
        f"la suite resuelve el jwt_secret en {secreto_de_la_suite}: lo lee del db_dir "
        "real y, si no existiera, lo CREA ahí (y la app lo adopta después)")

    real = cfg._default_db_dir() / "jwt_secret"
    assert real.resolve() != secreto_de_la_suite.resolve(), (
        f"el jwt_secret de la suite es el mismo archivo que el de producción ({real})")


def test_el_env_declara_la_raiz_y_las_hojas():
    """`MONITOR_DB_DIR` tiene que estar EXPORTADA, no sólo coincidir por casualidad
    (p.ej. porque la máquina no tenga LOCALAPPDATA y el default caiga en temp)."""
    from tests.conftest import _TEST_DB_ENV

    assert "MONITOR_DB_DIR" in _TEST_DB_ENV, (
        "tests/conftest.py no exporta MONITOR_DB_DIR: `settings.db_dir` queda a merced "
        "del default de la máquina (en Windows, %LOCALAPPDATA%\\monitor)")
    assert os.environ.get("MONITOR_DB_DIR") == _TEST_DB_ENV["MONITOR_DB_DIR"]
    assert _adentro_del_sandbox(_TEST_DB_ENV["MONITOR_DB_DIR"])


def test_el_guard_de_aislamiento_cubre_la_raiz(monkeypatch, tmp_path):
    """Si `db_dir` se escapa del sandbox, la colección tiene que ABORTAR.

    Sin `db_dir` en el guard, el próximo store que cuelgue directo de la raíz (como el
    `jwt_secret`, que no es un campo de `_DB_DERIVED`) entra sin que nada avise — es la
    misma forma en que se coló `history_state_dir` una ronda antes."""
    from types import SimpleNamespace

    afuera = tmp_path / "monitor_real_falso"
    falso = SimpleNamespace(db_dir=str(afuera), **{
        f: os.path.join(_TEST_DB_DIR, leaf) for f, leaf in cfg._DB_DERIVED.items()})
    monkeypatch.setattr(cfg, "settings", falso)

    with pytest.raises(RuntimeError, match="db_dir"):
        _assert_db_isolation()


def test_el_guard_acepta_la_raiz_igual_al_sandbox(monkeypatch):
    """Control positivo y borde real: `db_dir` **ES** el sandbox, no un hijo. Con la
    contención escrita como `startswith(raiz + os.sep)` a secas, el propio arreglo
    abortaba la colección entera."""
    from types import SimpleNamespace

    falso = SimpleNamespace(db_dir=_TEST_DB_DIR, **{
        f: os.path.join(_TEST_DB_DIR, leaf) for f, leaf in cfg._DB_DERIVED.items()})
    monkeypatch.setattr(cfg, "settings", falso)

    _assert_db_isolation()


def test_la_contencion_no_se_conforma_con_un_prefijo_de_string(monkeypatch):
    """`<sandbox>_otro` NO cae adentro de `<sandbox>`: la igualdad que hubo que aceptar
    para la raíz no puede degradar el chequeo a un `startswith` pelado."""
    from types import SimpleNamespace

    vecino = _TEST_DB_DIR + "_otro"
    falso = SimpleNamespace(db_dir=vecino, **{
        f: os.path.join(vecino, leaf) for f, leaf in cfg._DB_DERIVED.items()})
    monkeypatch.setattr(cfg, "settings", falso)

    with pytest.raises(RuntimeError):
        _assert_db_isolation()
