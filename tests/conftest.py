"""pytest config: agregar el repo root al sys.path para que los tests
puedan `from core.X import ...` sin instalar el paquete."""

import os
import sys
import tempfile

# AISLAMIENTO DE LA DB (crítico): apuntar catalog.db / analytics.duckdb a una
# carpeta temporal ANTES de cualquier import de core, para que NINGÚN test toque
# la `catalog.db` real. Sin esto, tests como `test_catalog_repository` (que llaman
# `ingest_from_excel(MASTER_XLSX)`) re-siembran la DB viva desde el Excel y BORRAN
# las hojas que no vienen del Excel (Obligaciones_Negociables, Acciones) en cada
# corrida. `settings` lee estos env (`MONITOR_*`) al importarse; `setdefault`
# respeta un override manual.
_TEST_DB_DIR = os.path.join(tempfile.gettempdir(), "monitor_pytest")
os.makedirs(_TEST_DB_DIR, exist_ok=True)
os.environ.setdefault("MONITOR_CATALOG_DB", os.path.join(_TEST_DB_DIR, "catalog.db"))
os.environ.setdefault("MONITOR_ANALYTICS_DUCKDB", os.path.join(_TEST_DB_DIR, "analytics.duckdb"))
os.environ.setdefault("MONITOR_PRICE_HISTORY_DB", os.path.join(_TEST_DB_DIR, "price_history.db"))

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Los tests levantan la app FastAPI vía TestClient; sin esto, los loops de
# refresh/BEI correrían pricing con índices reales en background y contaminarían
# los caches de módulo (avg TAMAR), rompiendo test_pricing_equivalence.
os.environ.setdefault("MONITOR_DISABLE_LOOPS", "1")
