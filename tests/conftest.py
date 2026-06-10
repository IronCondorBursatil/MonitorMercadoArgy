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
