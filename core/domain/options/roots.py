"""Mapping ROOT (3-4 letras) → ticker del subyacente (acción cotizante).

Los tickers de opciones BYMA codifican el root como las primeras 3 letras
(ej. GFG = Galicia → GGAL, ALU = Aluar → ALUA). No hay un endpoint público
con este mapping — se construye observando los símbolos que aparecen en el
endpoint arg_options y matcheando con los de arg_stocks.

Si aparecen nuevos roots no listados, simplemente se ignoran (la opción
queda sin spot y no se enriquece). Agregar acá conforme aparezcan.
"""
from __future__ import annotations

ROOT_TO_UNDERLYING: dict[str, str] = {
    "GFG": "GGAL",   # Grupo Financiero Galicia
    "ALU": "ALUA",   # Aluar
    "PAM": "PAMP",   # Pampa Energía
    "YPF": "YPFD",   # YPF
    "COM": "COME",   # Sociedad Comercial del Plata
    "BMA": "BMA",    # Banco Macro
    "TXR": "TXAR",   # Ternium Argentina
    "CEP": "CEPU",   # Central Puerto
    "TRA": "TRAN",   # Transener
    "MET": "METR",   # Metrogas
    "SUP": "SUPV",   # Grupo Supervielle
    "BBA": "BBAR",   # BBVA Argentina
    "MIR": "MIRG",   # Mirgor
    "CRE": "CRES",   # Cresud
    "EDN": "EDN",    # Edenor
    "TGS": "TGSU2",  # Transportadora de Gas del Sur
    "LOM": "LOMA",   # Loma Negra
    "BYM": "BYMA",   # BYMA (mercado)
    "BHI": "BHIP",   # Banco Hipotecario
    "TEC": "TECO2",  # Telecom Argentina
    "CEC": "CECO2",  # Central Costanera
}


def underlying_for(root: str) -> str | None:
    """Devuelve el ticker del subyacente, o None si el root no está mapeado."""
    return ROOT_TO_UNDERLYING.get(root.upper())
