"""Registry de pricing strategies: reemplaza la escalera `if _is_*_type()`
(deuda #1) por una tabla (predicado → strategy). Una instancia singleton por
familia (las strategies son stateless)."""

from __future__ import annotations

from typing import Callable, List, Tuple

from core.domain.pricing.base import PricingStrategy, VanillaStrategy
from core.domain.pricing.strategies import (
    CerStrategy,
    DolarLinkedStrategy,
    DualCerTamarStrategy,
    TamarStrategy,
)

# Orden = precedencia de ramas del `services.py` original. Los tipos del tope
# (DUAL_CER_TAMAR / PURO / DUAL / DOLAR_LINKED) son disjuntos entre sí, e is_cer
# excluye explícitamente las variantes TAMAR — así que el orden no causa
# misrouting; se mantiene "más específico primero" por claridad.
#
# El 30/360 NO es una regla acá: es un modificador de day-count inline que
# `VanillaStrategy` aplica en TIR/duración (igual que el original). BOPREAL y los
# CER 30/360 lo heredan vía VanillaStrategy / CerStrategy.
_RULES: List[Tuple[Callable, PricingStrategy]] = [
    (lambda i: i.is_dual_cer_tamar, DualCerTamarStrategy()),
    (lambda i: i.is_tamar_puro or i.is_dual_tamar, TamarStrategy()),
    (lambda i: i.is_dolar_linked, DolarLinkedStrategy()),
    (lambda i: i.is_cer, CerStrategy()),
]
_DEFAULT = VanillaStrategy()


def strategy_for(inst) -> PricingStrategy:
    for matches, strat in _RULES:
        if matches(inst):
            return strat
    return _DEFAULT
