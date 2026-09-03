"""Entry point: arranca el dashboard web FastAPI + HTMX vía uvicorn.

(Cutover de la reingeniería: reemplaza el http.server + SPA por apps.web.app.
El server viejo queda en el historial de git / branch master.)
"""

import sys

# --- Guard de versión de Python -------------------------------------------- #
# Pinneado a Python 3.12 (ver requirements.txt). El runtime es el Python de
# Programs: `py -3.12`, o %LOCALAPPDATA%\Programs\Python\Python312\python.exe
# (el "Store Python" ya no existe). Arrancá con run.bat, que lo resuelve solo.
if sys.version_info[:2] != (3, 12):
    raise SystemExit(
        f"[Monitor] Requiere Python 3.12.x — estás usando {sys.version.split()[0]}.\n"
        "Arrancá con run.bat (usa el intérprete correcto automáticamente)."
    )

import logging
import time

from config.settings import settings, setup_logging

setup_logging()
logger = logging.getLogger(__name__)


def run_web_supervised() -> None:
    """Levanta uvicorn; reinicia si cae inesperadamente. Ctrl+C sale limpio."""
    import uvicorn

    attempt = 0
    while True:
        attempt += 1
        try:
            # log_config=None → uvicorn no pisa nuestro setup_logging.
            uvicorn.run("apps.web.app:app", host=settings.host, port=settings.port,
                        log_config=None)
            logger.info("Web server stopped cleanly.")
            return
        except KeyboardInterrupt:
            logger.info("Ctrl+C received — exiting.")
            return
        except Exception:
            logger.exception(f"uvicorn crashed (attempt {attempt}). Restarting in 5s...")
            time.sleep(5)


if __name__ == "__main__":
    print("=" * 60)
    print("  MONITOR WEB — FastAPI + HTMX")
    print(f"  http://localhost:{settings.port}")
    print("  (Auto-restart si crashea. Ctrl+C para detener.)")
    print("=" * 60)
    run_web_supervised()
