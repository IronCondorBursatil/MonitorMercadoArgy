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
    history_dir: Path = _BASE_DIR / "data" / "history"
    # Bases .db FUERA del working tree: la catalog.db es la fuente de verdad y no debe
    # quedar donde corre git pull/clean (ver invariante en CLAUDE.md).
    db_dir: Path = Path(os.environ.get("LOCALAPPDATA", str(_BASE_DIR))) / "monitor"
    catalog_db: Path = db_dir / "catalog.db"
    # Backups recuperables de la catalog.db (fuente de verdad viva): snapshot online
    # 1×/día al arrancar, rota a `backup_keep` archivos por pool (daily y tagged por
    # separado — ver backup.py). Fuera del working tree.
    backup_dir: Path = db_dir / "backups"
    backup_keep: int = 7
    # Cierres diarios por ticker (variaciones Sem/1M/3M/YTD/1A). Se auto-mantiene
    # (priming Data912 historical + acumulación del feed vivo) — ver price_history.py.
    price_history_db: Path = db_dir / "price_history.db"
    # Histórico FCI (vcp/ccp/patrimonio por fondo) p/ flujos reales (Δccp×VCP). Se
    # auto-mantiene acumulando el corte diario de ArgentinaDatos — ver fci_history.py.
    fci_history_db: Path = db_dir / "fci_history.db"
    # Cierres diarios de índices BYMA p/ la franja de 5 ruedas del catálogo. M/G se
    # backfillean del chart; los 16 acumulan el cierre de /index-price — ver index_history.py.
    index_history_db: Path = db_dir / "index_history.db"
    index_ruedas: int = 5               # ventana del sparkline de índices (ruedas)

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

    host: str = "0.0.0.0"
    port: int = 8000
    refresh_sec: int = 5
    # Chain de opciones (parser + CRR + griegos de ~1000 contratos): ~5-20s según CPU
    # y cantidad. Fuera del refresh loop de precios (loop propio) para no espaciar el
    # push SSE de los paneles de bonos — nadie necesita los griegos cada 5s. Es CPU
    # puro (mantiene el GIL en `to_thread`), así que mientras corre ralentiza los
    # ciclos de precios; espaciarlo a 60s achica esa ventana de interferencia.
    options_refresh_sec: int = 60
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
        self.db_dir.mkdir(parents=True, exist_ok=True)
        if not self.jwt_secret_key:
            self.jwt_secret_key = _resolve_jwt_secret(self.db_dir)


settings = Settings()

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
