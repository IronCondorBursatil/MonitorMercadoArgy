"""pytest config: agregar el repo root al sys.path para que los tests
puedan `from core.X import ...` sin instalar el paquete."""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
