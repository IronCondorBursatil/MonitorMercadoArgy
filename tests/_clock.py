"""Fecha de referencia FIJA para los tests sensibles a la fecha (M0.1).

Los tests que prizan los instrumentos reales del master (test_pricing_equivalence)
usaban `date.today()` como settlement. A medida que los instrumentos vencen, sus
flujos se vacían y la comparación pierde sentido → la red de seguridad **se degrada
sola con el paso del tiempo**. Acá la fijamos a la fecha de captura de los golden de
Balanz (2026-06-10), cuando todo el universo está vivo.

La equivalencia (motor nuevo == motor legacy congelado) sigue siendo válida con
fecha fija: ambos motores ven la MISMA fecha, así que el test sigue detectando
cualquier divergencia de pricing — solo deja de depender del calendario.

Override con `MONITOR_TEST_REF_DATE`:
  - `YYYY-MM-DD` → fecha explícita.
  - `today`      → fecha real (opt-in para detectar regresiones date-dependent).
"""

from __future__ import annotations

import os
from datetime import date

DEFAULT_REF_DATE = date(2026, 6, 10)


def ref_date() -> date:
    raw = os.environ.get("MONITOR_TEST_REF_DATE")
    if not raw:
        return DEFAULT_REF_DATE
    if raw.strip().lower() == "today":
        return date.today()
    return date.fromisoformat(raw.strip())
