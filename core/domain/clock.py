"""Clock del dominio: `today()` overrideable por env (F1 del review).

El pricing core necesita "hoy" en tres lugares (ventana de promedio TAMAR,
extrapolación CER, síntesis de cashflows con `asof`). Con `date.today()` hardcodeado
esos cálculos son date-dependent y la red de regresión (equivalencia/golden) se
degrada con el reloj real aunque el settle esté congelado.

`MONITOR_AS_OF=YYYY-MM-DD` congela el "hoy" del dominio (lo usan los tests de
equivalencia vía tests/_clock.py). Sin el env → `date.today()`: en producción el
comportamiento es idéntico al anterior. Se lee en cada llamada (os.environ es un
dict — barato) para que monkeypatch.setenv por-test funcione sin recargas.
"""

from __future__ import annotations

import os
from datetime import date


def today() -> date:
    """Fecha "hoy" del dominio. Override por env MONITOR_AS_OF (ISO YYYY-MM-DD);
    levanta ValueError ante un valor malformado (mejor explotar que pricear con
    una fecha silenciosamente equivocada)."""
    raw = os.environ.get("MONITOR_AS_OF")
    if not raw:
        return date.today()
    return date.fromisoformat(raw.strip())
