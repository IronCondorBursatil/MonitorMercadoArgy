"""pytest config: agregar el repo root al sys.path para que los tests
puedan `from core.X import ...` sin instalar el paquete."""

import os
import sys
import tempfile

# AISLAMIENTO DE LA DB (crítico): apuntar las .db a una carpeta temporal ANTES de
# cualquier import de core, para que NINGÚN test toque la `catalog.db` real. Sin esto,
# tests como `test_catalog_repository` (que re-siembran desde el Excel) BORRAN las hojas
# que no vienen del Excel (Obligaciones_Negociables, Acciones) en cada corrida.
# `settings` lee estos env (`MONITOR_*`) al importarse; `setdefault` respeta un override manual.
_TEST_DB_DIR = os.path.join(tempfile.gettempdir(), "monitor_pytest")
os.makedirs(_TEST_DB_DIR, exist_ok=True)
os.environ.setdefault("MONITOR_CATALOG_DB", os.path.join(_TEST_DB_DIR, "catalog.db"))
os.environ.setdefault("MONITOR_PRICE_HISTORY_DB", os.path.join(_TEST_DB_DIR, "price_history.db"))
os.environ.setdefault("MONITOR_FCI_HISTORY_DB", os.path.join(_TEST_DB_DIR, "fci_history.db"))
os.environ.setdefault("MONITOR_INDEX_HISTORY_DB", os.path.join(_TEST_DB_DIR, "index_history.db"))
# Backups (M1.2): redirigir a temp. Sin esto, el backup del lifespan (que corre al
# bootear la app vía TestClient) escribiría en el dir REAL del usuario y, por la regla
# "uno por día", bloquearía el backup del catálogo real ese día.
os.environ.setdefault("MONITOR_BACKUP_DIR", os.path.join(_TEST_DB_DIR, "backups"))

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

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
