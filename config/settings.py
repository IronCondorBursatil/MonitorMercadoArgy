"""Configuración centralizada.

`settings` (pydantic-settings) es la fuente de verdad de paths y parámetros de
runtime. Agrega los paths de las bases `.db` **fuera del working tree de git** (la
`catalog.db` es la fuente de verdad: no debe vivir donde corre `git pull`/`git clean`).

Las constantes legacy (`BASE_DIR`, `DATA_DIR`, `MASTER_XLSX`) y `setup_logging()`
se conservan: las primeras como alias derivados de `settings` para no romper los
imports existentes mientras las fases migran a `settings.*`; el logging porque ya
mantiene el árbol liviano (RotatingFileHandler 5 MB).
"""

from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

# --------------------------------------------------------------------------- #
# .env loader (arbitrario): pydantic-settings sólo mapea los campos de Settings.
# Mantener este loader liviano carga cualquier KEY=VALUE extra (ej. API keys que
# otros módulos leen de os.environ) sin tener que declararlas en Settings.
# --------------------------------------------------------------------------- #
_BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Tiny .env loader. KEY=VALUE per line, # for comments. Skips existing env vars."""
    env_path = _BASE_DIR / ".env"
    if not env_path.is_file():
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


_load_dotenv()


def _default_db_dir() -> Path:
    """Directorio por defecto de las bases `.db`, **fuera del working tree de git**.

    `LOCALAPPDATA` sólo existe en Windows: usarlo como única fuente hacía que en
    Linux (el droplet) el default cayera en `<repo>/monitor`, o sea la `catalog.db`
    —fuente de verdad, con las altas del ABM que viven SOLO ahí—, los backups y el
    `jwt_secret` adentro del árbol donde `deploy.sh` corre `git pull`; un
    `git clean -xfd` se los lleva a todos juntos.

    En POSIX se usa `$XDG_DATA_HOME/monitor` o `~/.local/share/monitor`. Excepción
    deliberada: si YA existe un `<repo>/monitor/catalog.db` (droplet desplegado
    antes de este cambio) se lo respeta, porque mudarlo en silencio arrancaría con
    un catálogo VACÍO y dejaría la base viva huérfana. Ese caso lo denuncia el
    guard de `model_post_init` con un ERROR por ciclo de arranque.
    """
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "monitor"
    legacy = _BASE_DIR / "monitor"
    if (legacy / "catalog.db").is_file():
        return legacy
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "monitor"
    try:
        return Path.home() / ".local" / "share" / "monitor"
    except (RuntimeError, OSError):     # sin HOME (contenedor pelado)
        return legacy


# Nombre por defecto de cada base/directorio DENTRO de `db_dir`. Se resuelven en
# `model_post_init` (no en el cuerpo de la clase): así `MONITOR_DB_DIR` reubica
# TODO el conjunto —antes era una perilla muerta, había que enumerar 7 env vars—
# y agregar un store nuevo no exige actualizar ninguna receta de deploy.
_DB_DERIVED: dict[str, str] = {
    "catalog_db": "catalog.db",
    "backup_dir": "backups",
    "history_state_dir": "history",
    "price_history_db": "price_history.db",
    "fci_history_db": "fci_history.db",
    "ratings_history_db": "ratings_history.db",
    "index_history_db": "index_history.db",
}


def _inside(child: Path, parent: Path) -> bool:
    """True si `child` cae dentro de `parent` (comparando paths resueltos)."""
    try:
        return Path(child).resolve().is_relative_to(Path(parent).resolve())
    except (OSError, ValueError):   # p.ej. otra unidad en Windows
        return False


def _resolve_jwt_secret(db_dir: Path) -> str:
    """Secreto para firmar los JWT, sin default hardcodeado (el repo es público).
    Prioridad: archivo persistido en db_dir/jwt_secret (fuera del working tree, 0600) >
    generado al vuelo y persistido. El env MONITOR_JWT_SECRET_KEY ya lo resolvió antes
    (llena el campo → esta función no corre). Persistir evita invalidar las sesiones en
    cada reinicio; si no se puede escribir, se usa uno efímero (con WARNING)."""
    import secrets

    secret_file = db_dir / "jwt_secret"
    try:
        if secret_file.is_file():
            existing = secret_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError:
        pass
    generated = secrets.token_urlsafe(64)
    try:
        secret_file.write_text(generated, encoding="utf-8")
        try:
            os.chmod(secret_file, 0o600)   # no-op efectivo en Windows, correcto en Linux
        except OSError:
            pass
    except OSError:
        logging.getLogger(__name__).warning(
            "no se pudo persistir el secreto JWT en %s; se usa uno efímero "
            "(las sesiones se invalidan al reiniciar)", secret_file)
    return generated


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONITOR_", env_file=".env", extra="ignore")

    # Raíz del proyecto (donde vive este código).
    base_dir: Path = _BASE_DIR
    # Datos append-only versionados en el repo (CSVs, xlsx seed).
    data_dir: Path = _BASE_DIR / "data"
    master_xlsx: Path = _BASE_DIR / "data" / "instruments_master.xlsx"
    # SEMILLA versionada, read-only: bootstrap de un clon nuevo. La app NUNCA
    # escribe aca (ver history_state_dir).
    history_dir: Path = _BASE_DIR / "data" / "history"
    # Bases .db FUERA del working tree: la catalog.db es la fuente de verdad y no debe
    # quedar donde corre git pull/clean (ver invariante en CLAUDE.md).
    # `None` = derivar en model_post_init (default de `_default_db_dir()` / `db_dir`).
    # Todo lo que cuelga de db_dir se resuelve EN RUNTIME, no en el cuerpo de la clase:
    # de lo contrario `MONITOR_DB_DIR` no reubica nada (era una perilla muerta).
    db_dir: Path | None = None
    catalog_db: Path | None = None
    # Backups recuperables de la catalog.db (fuente de verdad viva): snapshot online
    # 1×/día al arrancar, rota a `backup_keep` archivos por pool (daily y tagged por
    # separado — ver backup.py). Fuera del working tree. → db_dir/backups
    backup_dir: Path | None = None
    # ESTADO de runtime de las series de indices (CER/TAMAR/A3500/reservas).
    # Va FUERA del working tree: estos CSV se reescriben en cada ciclo y, cuando
    # vivian en data/history/ (versionado), dejaban el arbol del droplet sucio de
    # forma permanente y `git pull` abortaba en cada deploy. Se siembra de
    # history_dir la primera vez y a partir de ahi acumula solo aca. → db_dir/history
    history_state_dir: Path | None = None
    backup_keep: int = 7
    # Cierres diarios por ticker (variaciones Sem/1M/3M/YTD/1A). Se auto-mantiene
    # (priming Data912 historical + acumulación del feed vivo) — ver price_history.py.
    price_history_db: Path | None = None
    # Ventana que se CONSERVA en price_history. El unico read-path
    # (`_hist_bases`) pide 400 dias y consume hasta 377; 420 deja margen para que la
    # poda horaria nunca corte por debajo de lo que el motor va a pedir en el mismo
    # tick. Sin poda el store crece ~54k filas/anio (~50 MB de RAM a 5 anios); con
    # ella el techo queda estable en ~127k filas (~12 MB).
    price_history_keep_days: int = 420
    # Histórico FCI (vcp/ccp/patrimonio por fondo) p/ flujos reales (Δccp×VCP). Se
    # auto-mantiene acumulando el corte diario de ArgentinaDatos — ver fci_history.py.
    fci_history_db: Path | None = None
    # Historial de calificaciones FIX SCR (snapshot diario + cambios up/down/watch) p/ el
    # badge de 7 días del panel ON. Lo acumula el loop diario — ver ratings_history.py.
    ratings_history_db: Path | None = None
    # Cierres diarios de índices BYMA p/ la franja de 5 ruedas del catálogo. M/G se
    # backfillean del chart; los 16 acumulan el cierre de /index-price — ver index_history.py.
    index_history_db: Path | None = None
    index_ruedas: int = 5               # ventana del sparkline de índices (ruedas)
    # Guard "nada de .db dentro del proyecto": por default DENUNCIA (ERROR al boot,
    # ver `_check_db_paths`) pero deja arrancar, porque un droplet desplegado antes
    # de este cambio ya tiene la base viva adentro del árbol y abortar lo dejaría
    # sin servicio. Con MONITOR_DB_IN_TREE_FATAL=true el invariante pasa a duro.
    db_in_tree_fatal: bool = False

    # Fuente de cotizaciones live (hot-path). Default BYMA open (público, ~20min
    # demora); el usuario puede pasar a 'byma_realtime' (clave .env) o 'data912'
    # (fallback) en runtime desde la UI. Override por env MONITOR_MARKET_SOURCE.
    market_source: str = "byma_open"  # 'byma_open' | 'byma_realtime' | 'data912'
    # Fuente de la chain de opciones. 'byma' = panel open /options (OI real +
    # underlyingSymbol/optionType/maturityDate autoritativos, roots-independiente
    # → más profundidad); 'data912' = endpoint arg_options (fallback automático).
    options_source: str = "byma"      # 'byma' | 'data912'
    # Catálogo BYMA (symbol→ISIN/emisor/tipo) para enriquecer la base de títulos.
    byma_catalog_csv: Path = _BASE_DIR / "data" / "byma" / "titulos_final.csv"
    # Client OAuth del addin BYMA. NO es un secreto del usuario: está embebido en el
    # .xll PÚBLICO de BYMA (las credenciales del usuario van aparte, en .env). Se
    # centraliza acá (en vez de hardcodeado en infra) y se puede override por env
    # MONITOR_BYMA_CLIENT_ID / MONITOR_BYMA_CLIENT_SECRET.
    byma_client_id: str = "excel-addin-bd-client-pkg"
    byma_client_secret: str = "20V4nt3k203xc31"

    # Clave secreta para firmar las cookies de sesión (JWT). NUNCA un default
    # hardcodeado: con el repo público, cualquiera forjaría un token de admin. Se
    # resuelve en model_post_init: env MONITOR_JWT_SECRET_KEY > archivo persistido
    # (db_dir/jwt_secret, fuera del working tree, 0600) > generado y persistido al vuelo.
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    # 24h (antes 7 días). Sin refresh token, un token robado vive esto — 1 día es un
    # balance razonable seguridad/UX. Override por MONITOR_JWT_ACCESS_TOKEN_EXPIRE_MINUTES.
    jwt_access_token_expire_minutes: int = 60 * 24
    # Cookie de sesión con flag Secure (solo viaja por HTTPS). Default False porque el
    # droplet sirve por HTTP (443 cerrado); poné MONITOR_COOKIE_SECURE=true en cuanto
    # tengas TLS (certbot/CF) — con Secure la cookie no se filtra en una request HTTP.
    cookie_secure: bool = False
    # Frontera de confianza del reverse proxy para el rate-limit del login: SÓLO se lee
    # el header X-Forwarded-For (que escribe el CLIENTE) si el peer TCP está en esta
    # lista — por default el nginx local del droplet. Lista separada por comas; vacío =
    # no confiar en ningún XFF (todo se imputa al peer). Override:
    # MONITOR_TRUSTED_PROXY_IPS. Lo lee `apps/web/routers/auth.py::_trusted_proxies`.
    # SUPUESTO DEL DEPLOY: UN solo proxy, y la ÚLTIMA entrada del XFF la agregó él
    # (nginx con `$proxy_add_x_forwarded_for`). Si algún día se mete un CDN delante,
    # esa última entrada pasa a ser la IP del CDN y el limiter vuelve a meter a todos
    # los usuarios en un bucket único (y podría bloquear a los legítimos).
    trusted_proxy_ips: str = "127.0.0.1,::1"

    # Zona horaria del PROCESO. El droplet corre en Etc/UTC y la app usa `datetime.now()`
    # / `date.today()` naive por todos lados, así que sin esto (a) el header muestra
    # 11:09 en vez de 08:09, y (b) —más grave— entre las 21:00 y las 24:00 de Buenos
    # Aires el "hoy" del dominio (settlement BYMA, cashflows, cierre del price history)
    # ya es el día siguiente. La aplica `apply_timezone()` al importar este módulo.
    timezone: str = "America/Argentina/Buenos_Aires"

    host: str = "0.0.0.0"
    port: int = 8000
    refresh_sec: int = 5
    # Chain de opciones (parser + CRR + griegos de ~1000 contratos): ~5-20s según CPU
    # y cantidad. Fuera del refresh loop de precios (loop propio) para no espaciar el
    # push SSE de los paneles de bonos — nadie necesita los griegos cada 5s. Es CPU
    # puro (mantiene el GIL en `to_thread`), así que mientras corre ralentiza los
    # ciclos de precios; espaciarlo a 60s achica esa ventana de interferencia.
    options_refresh_sec: int = 60
    # TTL de los paneles de renta variable de BYMA open que NINGUN panel del monitor
    # consume en vivo (`btnGeneral`, `btnCedears`): solo alimentan el reconcile del
    # catalogo y el sidebar del ABM. Son los dos POST mas pesados (page_size=5000,
    # ~10k filas entre ambos) y salian cada 5s. `btnLideres` NO entra en el cache
    # (lo consume `panel_lider`), ni los 3 de renta fija. 0 desactiva el cache.
    equities_refresh_sec: int = 30
    # Workers del thread pool del motor de pricing (por ciclo). El trabajo es
    # mayormente CPU (XIRR/root-finding) con algo de I/O cacheado; con el GIL, más
    # threads que cores rinde poco y en laptops chicas genera thrashing. Acotado a los
    # cores disponibles (antes era un 20 fijo). Override por MONITOR_ENGINE_WORKERS.
    engine_workers: int = min(8, (os.cpu_count() or 4))
    bei_refresh_sec: int = 300
    # Mantenimiento del store de precios: prime 1× + acumula cierre del feed. Diario
    # alcanza (la historia cambia 1×/rueda); la última escritura del día ≈ cierre.
    price_history_sec: int = 3600
    # Priming complementario vía series históricas de BYMA open para los tickers que
    # Data912 /historical NO cubre (bopreales, letras, ON, patas MEP/CABLE). Corre 1×.
    byma_history_enabled: bool = True
    byma_history_max_days: int = 400    # ~13 meses: cubre 1A (365d) + tolerancia
    byma_history_min_days: int = 20     # < N ruedas en el store → primar de BYMA
    byma_history_workers: int = 4       # concurrencia (cortés con BYMA open)
    # Fuente del backfill BYMA: 'chart' (endpoint chart OHLCV, 1 llamada/ticker,
    # rango largo, cubre patas D/C y letras) o 'series' (POST seriesHistoricas,
    # solo cierre, paginado 25d). Chart es estrictamente mejor (verificado en vivo).
    byma_history_source: str = "chart"  # 'chart' | 'series'

    def model_post_init(self, __context: Any) -> None:
        # 1. Resolver db_dir y todo lo que cuelga de él (un override por campo —
        #    MONITOR_CATALOG_DB y compañía— ya llegó lleno y gana).
        if self.db_dir is None:
            self.db_dir = _default_db_dir()
        for field, leaf in _DB_DERIVED.items():
            if getattr(self, field) is None:
                setattr(self, field, self.db_dir / leaf)
        # 2. Invariante "nada de .db dentro del proyecto".
        self._check_db_paths()
        # 3. Crear los directorios contenedores. `backup_dir`/`history_state_dir` ya
        #    los crean sus escritores, pero las .db se abren con sqlite3.connect()
        #    directo: sin el padre creado, seguir la receta de CLAUDE.md (paths por
        #    campo fuera del árbol) revienta con "unable to open database file".
        for d in (self.db_dir, self.catalog_db.parent, self.price_history_db.parent,
                  self.fci_history_db.parent, self.ratings_history_db.parent,
                  self.index_history_db.parent):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logging.getLogger(__name__).warning(
                    "no se pudo crear el directorio %s (%s); las bases que cuelguen "
                    "de ahí van a fallar al abrirse", d, exc)
        if not self.jwt_secret_key:
            self.jwt_secret_key = _resolve_jwt_secret(self.db_dir)

    def _check_db_paths(self) -> None:
        """Denuncia las bases que caen DENTRO del working tree de git.

        No es cosmético: la `catalog.db` es la fuente de verdad (las altas del ABM
        viven SOLO ahí, igual que las cuentas de usuario y los históricos que se
        acumulan rueda a rueda y no se backfillean). Adentro del árbol, un
        `git clean -xfd` para destrabar un `git pull` conflictivo —o un re-clone—
        se lleva catalog.db + backups/ + jwt_secret + los 4 históricos de un saque.
        """
        offenders = [p for p in (self.db_dir, *(getattr(self, f) for f in _DB_DERIVED))
                     if _inside(p, self.base_dir)]
        if not offenders:
            return
        msg = (
            "las bases de datos resuelven DENTRO del working tree de git (%s): %s. "
            "Un `git clean -xfd` o un re-clone las borra (catalog.db es la FUENTE DE "
            "VERDAD: las altas del ABM y los usuarios viven sólo ahí). Seteá "
            "MONITOR_DB_DIR —o los paths por campo— a un directorio fuera del árbol "
            "(p.ej. /var/lib/monitor) y movelas."
        )
        args = (self.base_dir, ", ".join(str(p) for p in offenders))
        if self.db_in_tree_fatal:
            raise RuntimeError(msg % args)
        logging.getLogger(__name__).error(msg, *args)


settings = Settings()


def apply_timezone() -> None:
    """Fija la zona horaria del proceso a `settings.timezone`.

    Se llama al importar este módulo a propósito: TODO entry point (run.py, uvicorn
    importando `apps.web.app`, los scripts de `scripts/`, pytest) importa `settings`
    antes de tocar una fecha, y `datetime.now()`/`date.today()` leen la TZ del proceso
    vía libc. Hacerlo sólo en run.py dejaría afuera a los scripts y al arranque directo
    por uvicorn — que es justamente como corre el droplet.

    **No-op en Windows**, y NO por comodidad: el CRT de MSVC sólo entiende el formato
    POSIX (`ART3`), no un nombre IANA, y ante un `TZ` que no puede parsear se planta en
    **UTC**. Exportar la variable ahí adelantaba 3hs la hora local de la máquina de
    desarrollo (verificado: 11:25 en vez de 08:25) — justo el bug que esto arregla, pero
    al revés. Windows ya corre en hora argentina por configuración del SO, así que
    dejarlo intacto es lo correcto; `time.tzset` es la sonda de "esto es Unix".
    """
    tz = (settings.timezone or "").strip()
    tzset = getattr(time, "tzset", None)
    if not tz or tzset is None:
        return
    os.environ["TZ"] = tz
    tzset()


apply_timezone()

# --------------------------------------------------------------------------- #
# Logging centralizado
# --------------------------------------------------------------------------- #
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_LOG_FILE = str(settings.base_dir / "monitores_global.log")


class _ConsoleFilter(logging.Filter):
    """Política de CONSOLA: solo lo accionable. (El archivo recibe WARNING+.)

    Deja pasar a la terminal únicamente:
      - WARNING / ERROR / CRITICAL  → fallas a resolver.
      - requests HTTP con error (uvicorn.access status >= 400).
      - cualquier record marcado explícito con extra={"console": True}.
    Oculta de la consola: el ruido por-ciclo — httpx/httpcore (200 OK por fetch),
    access 2xx/3xx, e INFO de arranque/app.

    Para forzar un INFO puntual a la terminal:  logger.info(msg, extra={"console": True})
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        if getattr(record, "console", False):
            return True
        if record.name == "uvicorn.access":
            # args = (client, method, path, http_ver, status); mostrar solo >=400.
            try:
                return int(record.args[4]) >= 400
            except (TypeError, IndexError, ValueError):
                return True
        return False  # resto de INFO/DEBUG → solo al archivo


def setup_logging():
    if logging.getLogger().handlers:
        return
    fmt = logging.Formatter(LOG_FORMAT)
    # ARCHIVO: solo WARNING+ — registro durable de PROBLEMAS (errores de conexión,
    # breakers, fallas de fetch) para post-mortem. Antes logueaba INFO+ (httpx/access
    # por ciclo) y crecía a varios MB de ruido. En operación normal casi no crece.
    # RotatingFileHandler: 5 MB × 5 backups (cap 25 MB, mantiene el árbol liviano).
    file_handler = RotatingFileHandler(
        _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.WARNING)
    # CONSOLA: solo lo accionable (ver _ConsoleFilter). El ruido va al archivo.
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(logging.INFO)
    stream_handler.addFilter(_ConsoleFilter())
    logging.basicConfig(level=LOG_LEVEL, handlers=[stream_handler, file_handler])
