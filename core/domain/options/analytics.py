"""Analytics avanzados de estrategias multi-leg vía optionlab.

`optionlab` modela la distribución del subyacente al `target_date` con Black-Scholes
lognormal e integra el payoff de la estrategia sobre esa densidad para derivar:

  - **probability_of_profit** — P(estrategia rentable al vto)
  - **expected_profit_if_profitable** — EV+ condicional al outcome favorable
  - **expected_loss_if_unprofitable** — EV− condicional al outcome desfavorable
  - **profit_ranges** — [(lo, hi), ...] precios del subyacente donde PnL > 0
  - **strategy_cost** — débito/crédito neto al armar

Importante: `optionlab` usa BS *para la distribución del subyacente* (válido como
proxy lognormal). NO lo usamos para *pricear* las opciones — sus precios vienen
ya del CRR americano del refresh loop (`core.domain.options.pricing.crr_american`).
La asunción BS solo afecta cuán "razonable" es el modelo del random walk del
subyacente; aceptable para AR retail.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Lazy import: si `optionlab` no está instalado, el wrapper devuelve None silenciosamente.
try:
    from optionlab import run_strategy as _ol_run_strategy
    _AVAILABLE = True
except ImportError:  # pragma: no cover
    _ol_run_strategy = None
    _AVAILABLE = False
    logger.warning("optionlab no instalado; analytics avanzados deshabilitados.")


def _resolve_premium(item, qty: int) -> float:
    """Precio de entrada para una leg: ask si long, bid si short.
    Fallback a mid o last si bid/ask son 0 (opción ilíquida)."""
    is_long = qty > 0
    primary = item.ask if is_long else item.bid
    if primary and primary > 0:
        return float(primary)
    if item.mid and item.mid > 0:
        return float(item.mid)
    if item.last and item.last > 0:
        return float(item.last)
    return 0.01  # nominal positivo (optionlab requiere premium > 0)


def run_analytics(legs: Iterable[dict], items_by_ticker: dict, spot: float,
                  atm_iv: Optional[float], r: float,
                  start_date: date, target_date: date) -> Optional[dict]:
    """Wrappea optionlab.run_strategy con nuestras estructuras de dominio.

    Args:
        legs: [{ticker, qty}, ...]. qty signada (>0 long, <0 short).
        items_by_ticker: {ticker → OptionItem} para resolver kind, strike, prices.
        spot: spot del subyacente.
        atm_iv: IV ATM de la chain (decimal). Si None usa 0.5 como fallback.
        r: tasa libre de riesgo (decimal anual).
        start_date, target_date: fechas (típicamente hoy + vto de los legs).

    Returns:
        dict con {prob_profit, ev_profit, ev_loss, profit_ranges, strategy_cost}
        o None si optionlab no está disponible o no hay legs válidas.
    """
    if not _AVAILABLE:
        return None
    if not spot or spot <= 0:
        return None

    strategy_legs = []
    for leg in legs:
        if not leg.get("qty"):
            continue
        item = items_by_ticker.get(leg.get("ticker", "").upper())
        if not item:
            continue
        qty = int(leg["qty"])
        strategy_legs.append({
            "type": "call" if item.kind == "C" else "put",
            "strike": float(item.strike),
            "premium": _resolve_premium(item, qty),
            "action": "buy" if qty > 0 else "sell",
            "n": abs(qty),
            "expiration": target_date,
        })

    if not strategy_legs:
        return None

    sigma = float(atm_iv) if atm_iv and atm_iv > 0 else 0.5
    inputs = {
        "stock_price": float(spot),
        "volatility": sigma,
        "interest_rate": float(r),
        "min_stock": float(spot) * 0.5,
        "max_stock": float(spot) * 1.5,
        "strategy": strategy_legs,
        "start_date": start_date,
        "target_date": target_date,
        "model": "black-scholes",
    }
    try:
        out = _ol_run_strategy(inputs)
    except Exception as e:  # noqa: BLE001 — analytics es opcional, no romper el endpoint
        logger.warning("optionlab.run_strategy falló: %s", e)
        return None

    # profit_ranges puede contener ±inf en estrategias con cola ilimitada
    # (long call → upper=+inf, long put → lower=-inf). Mapeamos inf → None
    # para que el JSON sea válido; el frontend lo renderiza como "∞".
    raw_ranges = getattr(out, "profit_ranges", None) or []
    return {
        "prob_profit": _safe_float(getattr(out, "probability_of_profit", None)),
        "ev_profit": _safe_float(getattr(out, "expected_profit_if_profitable", None)),
        "ev_loss": _safe_float(getattr(out, "expected_loss_if_unprofitable", None)),
        "profit_ranges": [[_safe_float(r[0]), _safe_float(r[1])] for r in raw_ranges],
        "strategy_cost": _safe_float(getattr(out, "strategy_cost", None)),
        "min_return": _safe_float(getattr(out, "minimum_return_in_the_domain", None)),
        "max_return": _safe_float(getattr(out, "maximum_return_in_the_domain", None)),
    }


def _safe_float(v) -> Optional[float]:
    """Convierte a float o devuelve None si no es finito."""
    if v is None:
        return None
    try:
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):  # NaN/inf
            return None
        return f
    except (TypeError, ValueError):
        return None
