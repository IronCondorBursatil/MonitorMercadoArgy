"""Entry point: arranca el dashboard web (auto-restart si crashea)."""

import sys

# --- Guard de versión de Python -------------------------------------------- #
# El proyecto está pinneado a Python 3.12 (ver requirements.txt). Correrlo con
# otra versión suele romper por wheels incompatibles (numpy/scipy/pandas) o por
# el lío "Microsoft Store Python vs Programs Python". Chequeo temprano con
# mensaje claro ANTES de importar la stack pesada, así el error se entiende.
if sys.version_info[:2] != (3, 12):
    raise SystemExit(
        f"[Monitor Balanz] Requiere Python 3.12.x — estás usando "
        f"{sys.version.split()[0]}.\n"
        "Arrancá con run.bat (usa py -3.12 automáticamente).\n"
        "Si no tenés Python 3.12 instalado: https://www.python.org/downloads/"
    )

import logging
import time

from config.settings import setup_logging
from apps.web.server import main as web_server_main

setup_logging()
logger = logging.getLogger(__name__)


def run_web_supervised():
    """Auto-restart the web server if it exits unexpectedly. Ctrl+C exits cleanly."""
    attempt = 0
    while True:
        attempt += 1
        try:
            web_server_main()
            logger.info("Web server stopped cleanly.")
            return
        except KeyboardInterrupt:
            logger.info("Ctrl+C received — exiting web supervisor.")
            return
        except Exception:
            logger.exception(f"Web server crashed (attempt {attempt}). Restarting in 5s...")
            time.sleep(5)


if __name__ == "__main__":
    print("=" * 60)
    print("  MONITOR WEB — Dashboard Interactivo")
    print("  http://localhost:8000")
    print("  (Auto-restart si crashea. Ctrl+C para detener.)")
    print("=" * 60)
    run_web_supervised()