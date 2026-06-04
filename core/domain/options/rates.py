"""Tasas implícitas anualizadas de opciones (la métrica más pedida en AR retail).

Tres lecturas:

1. **TNA bruta** = (bid / S) × 365/T_días
   Yield del premio recibido como % del spot, anualizado base 365.
   "¿Qué tasa me llevo si vendo este premio y el subyacente no se mueve?"

2. **TEA bruta** = (1 + bid/S)^(365/T) − 1
   Versión efectiva anual (capitalización) de la TNA bruta.

3. **TNA al strike** — el retorno si la opción se ejerce / no se ejerce:
   - Call OTM (K>S):  lanzada cubierta llamada → (K + bid − S) / S × 365/T
   - Call ITM (K≤S):  bid/S × 365/T (TNA bruta — el strike ya está adentro)
   - Put OTM (K<S):   cash-secured put no asignada → bid/K × 365/T
   - Put ITM (K≥S):   asignada → (bid − (K − S)) / S × 365/T (puede ser negativo)

Todas las tasas son **decimales** (0.40 = 40%). Calculan sobre `bid` por ser el
precio realizable cuando uno *lanza* la opción. Si bid=0 devuelve None.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ImpliedRates:
    tna_bruta: Optional[float]
    tea_bruta: Optional[float]
    tna_strike: Optional[float]


def compute_rates(bid: float, spot: float, strike: float, kind: str,
                  t_days: int, last: float = 0.0) -> ImpliedRates:
    """Devuelve TNA bruta, TEA bruta y TNA al strike.

    Usa `bid` como precio de venta del premio (lo que efectivamente recibís al
    lanzar). Si `bid<=0` (sin oferta de compra en pantalla) cae a `last` (último
    precio operado), así las opciones ilíquidas muestran al menos una tasa de
    referencia. Cualquier campo puede ser None si todos los precios son <=0 o
    si spot/strike/t_days son inválidos.
    """
    premium = bid if bid > 0 else last
    if not (premium > 0 and spot > 0 and strike > 0 and t_days > 0):
        return ImpliedRates(None, None, None)

    factor = 365.0 / t_days
    tna_bruta = (premium / spot) * factor

    try:
        tea_bruta = (1.0 + premium / spot) ** factor - 1.0
    except (OverflowError, ValueError):
        tea_bruta = None

    # TNA al strike: lectura adaptada a moneyness y tipo.
    if kind == "C":
        if strike > spot:
            tna_strike = (strike + premium - spot) / spot * factor
        else:
            tna_strike = (premium / spot) * factor
    else:  # put
        if strike < spot:
            tna_strike = (premium / strike) * factor
        else:
            tna_strike = (premium - (strike - spot)) / spot * factor

    return ImpliedRates(tna_bruta=tna_bruta, tea_bruta=tea_bruta, tna_strike=tna_strike)
