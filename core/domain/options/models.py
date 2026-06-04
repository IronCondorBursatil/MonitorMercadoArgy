"""Modelos de dominio para opciones.

OptionContract: contrato (parseado + estático, sin precios).
OptionLeg: pata de una estrategia (ticker + qty).

Los datos de mercado (bid/ask/iv/greeks/rates) van en `OptionItem` (chain.py)
que es la unidad enriquecida que consume la UI.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class OptionContract:
    """Contrato estático. No incluye precios; los datos de mercado se mergean
    en `OptionItem`."""
    ticker: str
    root: str
    underlying: str
    kind: str           # "C" call · "V" put
    strike: float
    month: int          # 1-12
    month_code: str     # como aparece en el ticker
    expiry: date

    @property
    def is_call(self) -> bool:
        return self.kind == "C"


@dataclass(frozen=True)
class OptionLeg:
    """Pata de estrategia. qty > 0 = long, qty < 0 = short."""
    ticker: str
    qty: int

    @property
    def is_long(self) -> bool:
        return self.qty > 0
