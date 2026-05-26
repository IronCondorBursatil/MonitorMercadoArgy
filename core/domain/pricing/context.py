"""PricingContext: estado inmutable de un cálculo de pricing.

Reemplaza los caches de módulo con lock de `services.py` (deuda #4): el `settle`
(o el `ref` para V.Téc) se resuelve una vez por el caller y se inyecta, junto con
los providers y el forecast TAMAR opcional.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from core.domain.pricing.protocols import FxProvider, IndicesProvider


@dataclass(frozen=True, slots=True)
class PricingContext:
    settle: date  # fecha de referencia del cálculo (settle para TIR/MD/precio; ref para V.Téc)
    indices: Optional[IndicesProvider] = None
    fx: Optional[FxProvider] = None
    tamar_forecast: Optional[float] = None
