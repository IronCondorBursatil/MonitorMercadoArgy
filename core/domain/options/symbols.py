"""Parser de tickers BYMA de opciones argentinas.

Formato: ROOT(3) + KIND(C|V) + STRIKE(4-5 chars, opcional `.`) + MONTH(1-2 chars).
Total: 10 caracteres (BYMA trunca a 10; cuando strike es de 5 dígitos el mes
queda en 1 letra).

Ejemplos:
  ALUC1000JU  → ALU + C + 1000  + JU   (strike entero, mes 2c)
  GFGC70553J  → GFG + C + 70553 + J    (strike 5 dígitos, mes 1c truncado)
  COMC33.0JU  → COM + C + 33.0  + JU   (strike con decimal explícito)
  BYMC287.5J  → BYM + C + 287.5 + J    (strike decimal, mes truncado)

El strike puede ser entero o decimal. Si es entero "muy grande" vs spot
(>5× spot), se asume decimal implícito y se divide /10 (ver resolve_strike).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Códigos de mes: 2 chars preferido, 1 char fallback (cuando el ticker se trunca).
# JN/JU son ambos junio (variantes históricas); MR/MA ambos marzo.
MONTH_CODES_2: dict[str, int] = {
    "EN": 1, "FE": 2, "MR": 3, "MA": 3, "AB": 4, "MY": 5,
    "JN": 6, "JU": 6, "JL": 7, "AG": 8, "SE": 9, "OC": 10, "NO": 11, "DI": 12,
}
MONTH_CODES_1: dict[str, int] = {
    "E": 1, "F": 2, "M": 3, "A": 4, "Y": 5, "J": 6, "L": 7,
    "G": 8, "S": 9, "O": 10, "N": 11, "D": 12,
}

# Ticker debe ser 9-11 chars (típico 10). C = call, V = put (BYMA).
_TICKER_RE = re.compile(r"^([A-Z]{3})([CV])([0-9]+(?:\.[0-9]*)?)([A-Z]{1,2})$")


@dataclass(frozen=True)
class OptionMeta:
    """Resultado del parser: campos crudos derivados del ticker."""
    ticker: str
    root: str
    kind: str           # "C" (call) o "V" (put)
    strike_str: str     # tal cual aparece en el ticker (puede traer ".")
    month_code: str     # "JU", "J", etc. (lo que aparece en el ticker)
    month: int          # 1-12

    @property
    def is_call(self) -> bool:
        return self.kind == "C"

    @property
    def is_put(self) -> bool:
        return self.kind == "V"


def parse_ticker(symbol: str) -> Optional[OptionMeta]:
    """Parsea un ticker BYMA de opción. Devuelve None si no matchea el formato."""
    if not symbol:
        return None
    s = symbol.upper().strip()
    m = _TICKER_RE.match(s)
    if not m:
        return None
    root, kind, strike_str, month_code = m.group(1), m.group(2), m.group(3), m.group(4)
    # Resolver mes: probar 2-char primero, caer a 1-char.
    month: Optional[int] = MONTH_CODES_2.get(month_code) if len(month_code) == 2 else None
    if month is None and len(month_code) >= 1:
        month = MONTH_CODES_1.get(month_code[0])
    if month is None:
        return None
    return OptionMeta(ticker=s, root=root, kind=kind,
                      strike_str=strike_str, month_code=month_code, month=month)


def resolve_strike(strike_str: str, spot: Optional[float]) -> Optional[float]:
    """Convierte el strike string a float. Aplica decimal implícito si es entero
    y resulta >>5× spot (heurística común en tickers BYMA truncados).
    """
    cleaned = strike_str.rstrip(".")
    if not cleaned:
        return None
    try:
        v = float(cleaned)
    except ValueError:
        return None
    if "." not in strike_str and spot and spot > 0 and v / spot > 5:
        v /= 10.0
    return v
