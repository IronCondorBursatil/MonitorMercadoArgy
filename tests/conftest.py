"""pytest config: agregar el repo root al sys.path para que los tests
puedan `from core.X import ...` sin instalar el paquete."""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Los tests levantan la app FastAPI vía TestClient; sin esto, los loops de
# refresh/BEI correrían pricing con índices reales en background y contaminarían
# los caches de módulo (avg TAMAR), rompiendo test_pricing_equivalence.
os.environ.setdefault("MONITOR_DISABLE_LOOPS", "1")
