"""Configuración centralizada.

`settings` (pydantic-settings) es la fuente de verdad de paths y parámetros de
runtime. Agrega los paths de las bases `.db` **fuera de OneDrive** (el sync de
OneDrive corrompe SQLite/DuckDB mid-write).

Las constantes legacy (`BASE_DIR`, `DATA_DIR`, `MASTER_XLSX`) y `setup_logging()`
se conservan: las primeras como alias derivados de `settings` para no romper los
imports existentes mientras las fases migran a `settings.*`; el logging porque ya
respeta el límite de tamaño de OneDrive (RotatingFileHandler 5 MB).
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONITOR_", env_file=".env", extra="ignore")

    # Raíz del proyecto (donde vive este código, en OneDrive).
    base_dir: Path = _BASE_DIR
    # Datos append-only que SÍ pueden vivir en OneDrive (CSVs, xlsx seed).
    data_dir: Path = _BASE_DIR / "data"
    master_xlsx: Path = _BASE_DIR / "data" / "instruments_master.xlsx"
    history_dir: Path = _BASE_DIR / "data" / "history"
    # Bases .db FUERA de OneDrive: el sync corrompe SQLite/DuckDB mid-write.
    db_dir: Path = Path(os.environ.get("LOCALAPPDATA", str(_BASE_DIR))) / "monitor"
    catalog_db: Path = db_dir / "catalog.db"
    analytics_duckdb: Path = db_dir / "analytics.duckdb"

    host: str = "127.0.0.1"
    port: int = 8000
    refresh_sec: int = 5
    bei_refresh_sec: int = 300

    def model_post_init(self, __context) -> None:
        self.db_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()

# --------------------------------------------------------------------------- #
# Alias legacy (str) — mantener hasta la limpieza de Fase 5. Los consumidores
# actuales (cartera_store, bei, cafci/indices providers, server, _common,
# data_quality_check) importan estos nombres como strings.
# --------------------------------------------------------------------------- #
BASE_DIR = str(settings.base_dir)
DATA_DIR = str(settings.data_dir)
MASTER_XLSX = str(settings.master_xlsx)

# --------------------------------------------------------------------------- #
# Logging centralizado
# --------------------------------------------------------------------------- #
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_LOG_FILE = str(settings.base_dir / "monitores_global.log")


class _ConsoleFilter(logging.Filter):
    """Política de CONSOLA: solo lo accionable. (El archivo recibe TODO.)

    Deja pasar a la terminal únicamente:
      - WARNING / ERROR / CRITICAL  → fallas a resolver.
      - requests HTTP con error (uvicorn.access status >= 400).
      - cualquier record marcado explícito con extra={"console": True}.
    Oculta de la consola (pero NO del archivo): el ruido por-ciclo —
    httpx/httpcore (200 OK por fetch), access 2xx/3xx, e INFO de arranque/app.

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
    # ARCHIVO: log COMPLETO (INFO+, incluye httpx/access) — registro durable para
    # debug. RotatingFileHandler: 5 MB × 5 backups (= 25 MB máx, respeta OneDrive).
    file_handler = RotatingFileHandler(
        _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.INFO)
    # CONSOLA: solo lo accionable (ver _ConsoleFilter). El ruido va al archivo.
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(logging.INFO)
    stream_handler.addFilter(_ConsoleFilter())
    logging.basicConfig(level=LOG_LEVEL, handlers=[stream_handler, file_handler])
