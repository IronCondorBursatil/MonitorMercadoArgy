"""pytest config: agregar el repo root al sys.path para que los tests
puedan `from core.X import ...` sin instalar el paquete."""

import os
import shutil
import sys
import tempfile

# AISLAMIENTO DE LA DB (crítico): apuntar las .db a una carpeta temporal ANTES de
# cualquier import de core, para que NINGÚN test toque la `catalog.db` real. Sin esto,
# tests como `test_catalog_repository` (que re-siembran desde el Excel) BORRAN las hojas
# que no vienen del Excel (Obligaciones_Negociables, Acciones) en cada corrida.
# `settings` lee estos env (`MONITOR_*`) al importarse.
#
# INCONDICIONAL, a propósito (antes era `setdefault`): si la shell ya traía
# `MONITOR_CATALOG_DB` —una terminal donde se exportó para apuntar la app a otra base,
# el EnvironmentFile del droplet— el setdefault era un no-op y la suite escribía sobre
# ESA base: la fixture `users` de test_auth.py hace `query(UserORM).delete()` (borra
# TODAS las cuentas) y test_catalog_router/test_byma_ficha_meta dejan tickers ficticios
# en el catálogo VIVO, que es la fuente de verdad (CLAUDE.md). Un override manual acá
# no vale el riesgo: para apuntar a otra base, un test usa la fixture `tmp_db`.
#
# `MONITOR_TEST_DB_DIR` (perilla SOLO de tests): dos tests lanzan `pytest` adentro de
# pytest (test_aud_G_tests_db_isolation / test_aud_G_tests_equivalence_guard). Esos
# subprocesos re-importan este conftest y, con el dir FIJO, purgaban el sandbox de la
# sesión PADRE mientras ésta tenía la catalog.db abierta — en Windows el lock del SO lo
# hacía inofensivo, pero en POSIX el unlink procede y le saca la base de abajo. Con esta
# variable cada subproceso corre en su propio sandbox. Se acepta SOLO si cae dentro de
# %TEMP%: así la perilla no puede usarse para apuntar la suite a una base real.
_TEST_DB_DIR = os.path.join(tempfile.gettempdir(), "monitor_pytest")
_TEST_DB_DIR_OVERRIDE = os.environ.get("MONITOR_TEST_DB_DIR")
if _TEST_DB_DIR_OVERRIDE and os.path.realpath(_TEST_DB_DIR_OVERRIDE).startswith(
        os.path.realpath(tempfile.gettempdir()) + os.sep):
    _TEST_DB_DIR = _TEST_DB_DIR_OVERRIDE
os.makedirs(_TEST_DB_DIR, exist_ok=True)
_TEST_DB_ENV = {
    # Raíz de TODO lo que cuelga de `db_dir`. Los campos de abajo ya van con override
    # propio (y el override por campo GANA sobre la derivación), pero `db_dir` en sí
    # tiene consumidores directos que ningún override por campo alcanza: el más
    # importante es el `jwt_secret` (`settings.model_post_init` →
    # `_resolve_jwt_secret(self.db_dir)`), que LEE —y si no existe, CREA con 0600— el
    # archivo `db_dir/jwt_secret`. Sin esta línea `settings.db_dir` resolvía al
    # directorio REAL del usuario (`%LOCALAPPDATA%\monitor`, verificado en vivo): la
    # suite firmaba sus JWT con el secreto de producción y, en una máquina/CI donde ese
    # archivo todavía no existiera, correr los tests lo generaba adentro del db_dir real
    # (adoptándolo después la app). Redirigirlo también deja al guard
    # `_assert_db_isolation()` cubriendo la raíz, no sólo las hojas.
    "MONITOR_DB_DIR": _TEST_DB_DIR,
    "MONITOR_CATALOG_DB": os.path.join(_TEST_DB_DIR, "catalog.db"),
    "MONITOR_PRICE_HISTORY_DB": os.path.join(_TEST_DB_DIR, "price_history.db"),
    "MONITOR_FCI_HISTORY_DB": os.path.join(_TEST_DB_DIR, "fci_history.db"),
    "MONITOR_INDEX_HISTORY_DB": os.path.join(_TEST_DB_DIR, "index_history.db"),
    "MONITOR_RATINGS_HISTORY_DB": os.path.join(_TEST_DB_DIR, "ratings_history.db"),
    # Estado de runtime de las series (CER/TAMAR/A3500/reservas/BEI/CAFCI). Sin esto la
    # suite REESCRIBE los CSV reales de `%LOCALAPPDATA%\monitor\history` (verificado):
    # cualquier test que ejercite indices_provider/cafci_provider/bei pisa el estado vivo
    # del usuario con lo que haya fetcheado —o mockeado— el test. Es un campo propio de
    # `Settings`: va con override explícito además del MONITOR_DB_DIR de arriba, porque el
    # override por campo GANA sobre la derivación y así el redirect no depende de él.
    # Los lectores caen a la SEMILLA versionada (`data/history/`) cuando el estado falta
    # (ver core/infrastructure/history_paths.resolve_read), así que los tests siguen
    # leyendo datos deterministas del repo.
    "MONITOR_HISTORY_STATE_DIR": os.path.join(_TEST_DB_DIR, "history"),
    # Backups (M1.2): redirigir a temp. Sin esto, el backup del lifespan (que corre al
    # bootear la app vía TestClient) escribiría en el dir REAL del usuario y, por la regla
    # "uno por día", bloquearía el backup del catálogo real ese día.
    "MONITOR_BACKUP_DIR": os.path.join(_TEST_DB_DIR, "backups"),
}
os.environ.update(_TEST_DB_ENV)


def _purge_test_dbs(db_dir):
    """Borra las .db de la corrida ANTERIOR (con sus -wal/-shm).

    El dir es fijo, así que sin esto la `catalog.db` de pytest sobrevive entre
    corridas — y `CatalogRepository` siembra del Excel SOLO si la tabla está vacía:
    el universo de instrumentos queda congelado en el `instruments_master.xlsx` del
    día en que esa máquina corrió la suite por primera vez (editar el master no se
    refleja en los tests) y encima acumula las filas que dejan los tests que escriben
    sobre el engine por defecto. Sembrar de cero cuesta ~0.5s.

    Tolerante a fallas: si otra corrida en paralelo tiene la base abierta, Windows
    no deja borrarla → se saltea en silencio en vez de reventar la colección."""
    try:
        names = os.listdir(db_dir)
    except OSError:
        return
    for name in names:
        if ".db" not in name:
            continue
        try:
            os.remove(os.path.join(db_dir, name))
        except OSError:
            pass          # base tomada por otra corrida (o no es un archivo): se deja


_purge_test_dbs(_TEST_DB_DIR)
# Mismo motivo que la purga de arriba, para el estado de las series: los CSV que un
# test deje en el sandbox (`resolve_read` prefiere el estado a la semilla) sobrevivirían
# a la corrida y la siguiente leería ESO en vez de `data/history/`, que es la semilla
# determinista del repo. Se borra entero: los escritores lo recrean (`os.makedirs`).
shutil.rmtree(_TEST_DB_ENV["MONITOR_HISTORY_STATE_DIR"], ignore_errors=True)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# Fallback del listado de stores, por si `config.settings._DB_DERIVED` se renombra.
# Manual = se desactualiza: el `history_state_dir` faltó acá durante toda una ronda de
# auditoría y la suite estuvo reescribiendo los CSV reales del usuario sin que nada
# avisara. Por eso el guard prefiere la fuente única de `Settings`.
_STORES_FALLBACK = ("cartera_json", "log_file",
                    "catalog_db", "backup_dir", "history_state_dir", "price_history_db",
                    "fci_history_db", "ratings_history_db", "index_history_db")


def _assert_db_isolation():
    """Guard DURO: aborta la colección si `settings` resolvió alguna base fuera del
    dir temporal. Es la red del bloque de arriba — si mañana alguien agrega una base
    nueva a `Settings` y se olvida de redirigirla, la suite deja de correr en vez de
    escribir en el `db_dir` real del usuario.

    Los campos NO van hardcodeados: se leen de `config.settings._DB_DERIVED`, que es la
    fuente única de todo lo que cuelga de `db_dir`. Con la lista copiada acá, un store
    nuevo entraba silenciosamente sin redirigir (fue lo que pasó con `history_state_dir`)
    y el guard —cuyo contrato es justamente cubrir ese caso— no lo veía."""
    import config.settings as _cfg
    from config.settings import settings
    # `db_dir` primero: es la RAÍZ (y el único hogar del `jwt_secret`, que no es un
    # campo de `_DB_DERIVED` y por eso no lo cubría ningún override por campo).
    campos = ("db_dir",) + tuple(getattr(_cfg, "_DB_DERIVED", None) or _STORES_FALLBACK)
    raiz = os.path.realpath(_TEST_DB_DIR)

    def _adentro(path) -> bool:
        # `db_dir` ES la raíz, así que la contención tiene que aceptar la igualdad;
        # las hojas siguen exigiendo el separador (para que `..._pytest_otro` no pase
        # por prefijo de string).
        real = os.path.realpath(str(path))
        return real == raiz or real.startswith(raiz + os.sep)

    fuera = {
        name: str(path) for name, path in (
            (name, getattr(settings, name)) for name in campos
        ) if not _adentro(path)
    }
    if fuera:
        raise RuntimeError(
            "ABORTADO: la suite apunta a bases FUERA de %s — correrla borraría "
            "usuarios y ensuciaría el catálogo vivo (fuente de verdad): %s"
            % (_TEST_DB_DIR, fuera))


_assert_db_isolation()

# Los tests levantan la app FastAPI vía TestClient; sin esto, los loops de
# refresh/BEI correrían pricing con índices reales en background y contaminarían
# los caches de módulo (avg TAMAR), rompiendo test_pricing_equivalence.
os.environ.setdefault("MONITOR_DISABLE_LOOPS", "1")


# --------------------------------------------------------------------------- #
# Helpers compartidos (antes copiados por archivo — un fix de flakiness o de
# teardown se aplica acá UNA vez y lo heredan todos los tests).
# --------------------------------------------------------------------------- #

import socket  # noqa: E402

import pytest  # noqa: E402


def listening_socket():
    """Socket TCP escuchando en un puerto efímero de localhost. Para los guards
    de server-vivo (ingest_master / restore_catalog): abierto = monitor 'corriendo';
    cerrarlo deja un puerto que se sabe libre. El caller hace s.close()."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s


@pytest.fixture
def tmp_db(tmp_path):
    """Aísla el engine SQLite a una DB temporal y lo restaura al salir.

    `configure()` dispone el engine anterior → el archivo temp queda cerrado
    antes del cleanup de tmp_path (sin locks en Windows). Imports adentro:
    `settings`/`core` deben importarse DESPUÉS de los env MONITOR_* de arriba."""
    from config.settings import settings
    from core.infrastructure.db import engine as db_engine
    db_engine.configure(tmp_path / "test.db")
    try:
        yield tmp_path
    finally:
        db_engine.configure(settings.catalog_db)


# --------------------------------------------------------------------------- #
# Bypass de auth (los 3 commits de auth cablearon RequireTabPermission en los 12
# routers pero no tocaron tests/ → 87 tests recibían el HTML de /login). Por
# defecto los tests corren como un admin autenticado: overrideamos las deps hoja
# de auth (get_current_user*) por un admin falso; RequireTabPermission.__call__ y
# get_admin_user* pasan por el short-circuit de is_admin. Los tests que ejercen la
# auth REAL (login/permisos) marcan @pytest.mark.noauth para NO recibir el bypass.
# Function-scoped: se re-aplica en cada test, así los `dependency_overrides.clear()`
# de teardown de otros tests no lo dejan sin efecto para el resto de la corrida.
# --------------------------------------------------------------------------- #
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "noauth: correr el test SIN el bypass de auth (auth real)")


class _FakeAdminUser:
    id = 1
    username = "test-admin"
    is_admin = True
    allowed_tabs = ["*"]
    hashed_password = ""


@pytest.fixture(autouse=True)
def _auth_bypass(request, monkeypatch):
    if "noauth" in request.keywords:
        yield
        return
    from apps.web.app import app
    from apps.web import deps_auth
    fake = _FakeAdminUser()
    overrides = {
        deps_auth.get_current_user: lambda: fake,
        deps_auth.get_current_user_html: lambda: fake,
        deps_auth.get_admin_user: lambda: fake,
        deps_auth.get_admin_user_html: lambda: fake,
    }
    app.dependency_overrides.update(overrides)
    # El contexto del template resuelve el usuario aparte de las dependencias
    # (templates.py llama a _get_user_from_token directo, no vía Depends) → sin esto
    # `has_tab()` devuelve False y el nav renderiza vacío en los tests.
    monkeypatch.setattr("apps.web.templates._get_user_from_token",
                        lambda request, db=None: fake, raising=False)
    try:
        yield
    finally:
        for key in overrides:
            app.dependency_overrides.pop(key, None)
