"""Currency helpers — ticker suffix → moneda canónica.

Convención: sufijo D=MEP, C=CABLE, resto=ARS.  Aplica a soberanos,
BOPREALES y ONs multi-pata. Un único punto de verdad para que repositorios,
infraestructura y capa web no re-implementen la misma regla.
"""
from __future__ import annotations


def ccy_from_suffix(ticker: str) -> str:
    """Moneda de una especie por su sufijo: D=MEP, C=CABLE, resto=ARS."""
    t = (ticker or "").upper()
    if t.endswith("D"):
        return "MEP"
    if t.endswith("C"):
        return "CABLE"
    return "ARS"
