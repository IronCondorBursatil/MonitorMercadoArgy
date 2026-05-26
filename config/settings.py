import os
import logging
from logging.handlers import RotatingFileHandler

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv():
    """Tiny .env loader. KEY=VALUE per line, # for comments. Skips existing env vars."""
    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(env_path):
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
DATA_DIR = os.path.join(BASE_DIR, "data")
MASTER_XLSX = os.path.join(DATA_DIR, "instruments_master.xlsx")

# Logging centralizado
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Ruta del log file — relativa al BASE_DIR del proyecto, no al cwd.
_LOG_FILE = os.path.join(BASE_DIR, "monitores_global.log")


def setup_logging():
    if logging.getLogger().handlers:
        return
    # RotatingFileHandler: máx. 5 MB por archivo, 5 backups (= 25 MB máximo).
    # Evita que el log crezca indefinidamente en producción 24×7.
    file_handler = RotatingFileHandler(
        _LOG_FILE,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.basicConfig(
        level=LOG_LEVEL,
        handlers=[stream_handler, file_handler],
    )
