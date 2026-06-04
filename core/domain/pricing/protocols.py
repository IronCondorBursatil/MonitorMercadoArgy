"""Contratos de los providers que el pricing consume (deuda #2: duck-typing →
Protocol). Cualquier objeto con estos métodos es un provider válido."""

from __future__ import annotations

from datetime import date
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class IndicesProvider(Protocol):
    def get_cer(self, target: date) -> Optional[float]: ...
    def get_tamar(self, target: Optional[date] = None) -> Optional[float]: ...


@runtime_checkable
class FxProvider(Protocol):
    def get_mayorista_venta(self) -> Optional[float]: ...
    # Offers MEP/CCL para la pata pesos de ONs hard-dollar (ley AR → MEP, NY → CCL).
    # Opcionales: las strategies los leen vía getattr (providers viejos sin estos
    # métodos degradan a None → la pata …O no se convierte, igual que antes).
    def get_mep_venta(self) -> Optional[float]: ...
    def get_ccl_venta(self) -> Optional[float]: ...
