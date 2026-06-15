"""Conversión de pata pesos → USD para display (no para el motor de pricing).

El motor (strategies.py) tiene su propia lógica de FX — este módulo es
exclusivamente para la capa web cuando necesita expresar en USD una pata
pesos de un instrumento multi-moneda (ONs, soberanos pata ARS).

Regla:
  hard-dollar ley AR  → MEP (get_mep_venta)
  hard-dollar ley EXT → CCL (get_ccl_venta)
  dollar-linked       → mayorista (get_mayorista_venta)
  cualquier otro      → None (no aplica conversión)
"""
from __future__ import annotations

from typing import Optional


def peso_leg_to_usd(inst, price_ars: Optional[float], fx) -> Optional[float]:
    """Convierte `price_ars` a USD usando el tipo de cambio adecuado para `inst`.

    Devuelve None si:
    - `price_ars` o `fx` son None
    - el instrumento no tiene pata en pesos (already USD-quoted)
    - el provider FX falla o retorna un valor inválido
    """
    if price_ars is None or fx is None:
        return None
    if inst.is_hard_dollar:
        meth = "get_mep_venta" if inst.is_ley_argentina else "get_ccl_venta"
    elif inst.is_dolar_linked:
        meth = "get_mayorista_venta"
    else:
        return None
    fn = getattr(fx, meth, None)
    try:
        rate = fn() if fn else None
    except Exception:  # noqa: BLE001 — provider caído → sin conversión
        return None
    if not rate or rate <= 0:
        return None
    return price_ars / rate
