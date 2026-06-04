"""Estrategias de opciones: payoff al vencimiento + presets típicos.

Una estrategia es una lista de patas (`OptionLeg`) — ticker + cantidad signada
(qty > 0 = long, qty < 0 = short). El payoff @ vto es la suma de payoffs
individuales menos la prima neta pagada/recibida.

Presets disponibles (devuelven ticker + qty sugeridos para un universo dado):
  - long_call, long_put
  - short_call, short_put (ventas desnudas — vista de prima/income; herramienta de
    análisis de payoff, no de order-entry)
  - covered_call (lanzada cubierta = short call OTM)
  - bull_call_spread, bear_put_spread
  - straddle, strangle
  - iron_condor

Cada preset elige los strikes más cercanos a un target (típicamente ATM, ATM±k%).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from core.domain.options.chain import OptionItem
from core.domain.options.models import OptionLeg


@dataclass(frozen=True)
class PayoffResult:
    """Curva de payoff + estadísticas."""
    xs: list[float]               # precios del subyacente al vto
    ys: list[float]               # PnL en cada x
    cost: float                   # prima neta (negativa = crédito recibido)
    max_gain: float
    max_loss: float
    breakevens: list[float]
    delta_net: Optional[float]
    gamma_net: Optional[float]
    theta_net: Optional[float]
    vega_net: Optional[float]


def _leg_payoff(item: OptionItem, qty: int, S: float) -> float:
    """Payoff de una pata al vto: qty × (intrinsic − entry_price)."""
    intrinsic = max(0.0, S - item.strike) if item.kind == "C" else max(0.0, item.strike - S)
    entry = item.ask if qty > 0 else item.bid
    return qty * (intrinsic - entry)


def payoff_curve(legs: Iterable[OptionLeg], items_by_ticker: dict[str, OptionItem],
                 spot: float, n_points: int = 80,
                 lo_factor: float = 0.6, hi_factor: float = 1.4) -> PayoffResult:
    """Genera curva de PnL @ vto + griegos netos. Si una leg no encuentra item
    se ignora silenciosamente (UI ya debería filtrar previamente)."""
    valid = [(leg.qty, items_by_ticker[leg.ticker])
             for leg in legs if leg.ticker in items_by_ticker and leg.qty != 0]
    if not valid or spot <= 0:
        return PayoffResult([], [], 0.0, 0.0, 0.0, [], None, None, None, None)

    lo, hi = spot * lo_factor, spot * hi_factor
    xs = [lo + (hi - lo) * i / (n_points - 1) for i in range(n_points)]
    ys = [sum(_leg_payoff(it, q, S) for q, it in valid) for S in xs]

    cost = 0.0
    d = g = th = v = 0.0
    have_d = have_g = have_th = have_v = False
    for q, it in valid:
        entry = it.ask if q > 0 else it.bid
        cost += q * entry
        if it.greeks.delta is not None:
            d += q * it.greeks.delta; have_d = True
        if it.greeks.gamma is not None:
            g += q * it.greeks.gamma; have_g = True
        if it.greeks.theta is not None:
            th += q * it.greeks.theta; have_th = True
        if it.greeks.vega is not None:
            v += q * it.greeks.vega;  have_v = True

    max_gain = max(ys); max_loss = min(ys)
    # Break-evens: cruces por cero (interpolación lineal).
    bes: list[float] = []
    for i in range(1, len(ys)):
        if (ys[i - 1] < 0 < ys[i]) or (ys[i - 1] > 0 > ys[i]):
            t = ys[i - 1] / (ys[i - 1] - ys[i])
            bes.append(xs[i - 1] + t * (xs[i] - xs[i - 1]))

    return PayoffResult(
        xs=xs, ys=ys, cost=cost, max_gain=max_gain, max_loss=max_loss,
        breakevens=bes,
        delta_net=d if have_d else None,
        gamma_net=g if have_g else None,
        theta_net=th if have_th else None,
        vega_net=v if have_v else None,
    )


# ── Presets ───────────────────────────────────────────────────────────────────

def _pick(items: list[OptionItem], kind: str, target: float) -> Optional[OptionItem]:
    """Devuelve el item del kind dado con strike más cercano a `target`."""
    cands = [it for it in items if it.kind == kind]
    if not cands:
        return None
    return min(cands, key=lambda it: abs(it.strike - target))


def preset_legs(name: str, items: list[OptionItem], spot: float) -> list[OptionLeg]:
    """Devuelve legs sugeridos para un preset. `items` debe estar filtrado a
    una chain (mismo underlying + mismo vto). Si los strikes target no existen,
    el preset devuelve menos legs (o lista vacía)."""

    def leg(kind: str, target: float, qty: int) -> Optional[OptionLeg]:
        it = _pick(items, kind, target)
        return OptionLeg(ticker=it.ticker, qty=qty) if it else None

    builders = {
        "long_call":    lambda: [leg("C", spot * 1.02,  1)],
        "long_put":     lambda: [leg("V", spot * 0.98,  1)],
        "short_call":   lambda: [leg("C", spot * 1.02, -1)],
        "short_put":    lambda: [leg("V", spot * 0.98, -1)],
        "covered_call": lambda: [leg("C", spot * 1.03, -1)],  # short call OTM (lanzada)
        "bull_call_spread": lambda: [
            leg("C", spot,         1), leg("C", spot * 1.08, -1)],
        "bear_put_spread": lambda: [
            leg("V", spot,         1), leg("V", spot * 0.92, -1)],
        "straddle": lambda: [
            leg("C", spot,         1), leg("V", spot,         1)],
        "strangle": lambda: [
            leg("C", spot * 1.05,  1), leg("V", spot * 0.95,  1)],
        "iron_condor": lambda: [
            leg("C", spot * 1.10,  1), leg("C", spot * 1.05, -1),
            leg("V", spot * 0.90,  1), leg("V", spot * 0.95, -1)],
    }
    builder = builders.get(name)
    if not builder:
        return []
    return [l for l in builder() if l is not None]


PRESET_NAMES = (
    "long_call", "long_put", "short_call", "short_put", "covered_call",
    "bull_call_spread", "bear_put_spread",
    "straddle", "strangle", "iron_condor",
)
