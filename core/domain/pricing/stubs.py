"""Stubs de providers para escenarios de revaluación.

`ZeroTamar` fuerza TAMAR=0 de modo que `max(TAMAR, floor)` resuelva siempre al
floor — usado para revaluar un DUAL como si sólo corriera la tasa fija
(`recompute_as_tasa_fija` y la pata TF de la calculadora). Reemplaza la clase
local `_ZeroTamar` que se definía inline en `services.py`.
"""

from __future__ import annotations

from datetime import date
from typing import Optional


class ZeroTamar:
    _cache_tamar: dict = {}

    def get_tamar(self, target: Optional[date] = None) -> float:
        return 0.0

    def get_cer(self, target: Optional[date] = None) -> Optional[float]:
        return None
