"""Opciones argentinas (BYMA): parser de tickers, pricing CRR americano, griegos,
tasas implícitas, builder de estrategias.

Data en vivo viene del endpoint data912.com/live/arg_options vía el ProviderHub.
Spots de subyacentes vienen de arg_stocks (mismo hub). No hay persistencia en
SQLite — todo es live + computado a partir del snapshot.
"""
from core.domain.options.analytics import run_analytics
from core.domain.options.chain import OptionItem, build_options
from core.domain.options.models import OptionLeg
from core.domain.options.pricing import crr_american, iv_implied
from core.domain.options.strategies import (
    PayoffResult, payoff_curve, preset_legs,
)
from core.domain.options.symbols import OptionMeta, parse_ticker

__all__ = [
    "OptionItem", "OptionLeg", "OptionMeta", "PayoffResult",
    "build_options", "crr_american", "iv_implied",
    "parse_ticker", "payoff_curve", "preset_legs",
    "run_analytics",
]
