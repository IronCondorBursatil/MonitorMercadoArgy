"""FinancialEngine — fachada delgada sobre el pricing core.

La escalera `if _is_*_type()` que vivía acá se reemplazó por un registry de
`PricingStrategy` (ver `core/domain/pricing/`). Esta clase preserva las firmas
públicas históricas (sus consumidores: `generate_report`, `bond_detail`,
`server`, `bei`) y delega:

  - dispatch por tipo  → `pricing.registry.strategy_for`
  - cálculo            → `PricingStrategy.{technical_value,tir,duration,price_from_tir}`
  - métricas de popup  → `pricing.metrics`
  - conversiones/tasas → `conventions`
  - XIRR               → `xirr`

`_is_cer_type` y `_cer_reference_date` se re-exportan a nivel módulo: `bond_detail`
los importa directamente.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

import numpy as np

from core.domain import conventions
from core.domain.conventions import cer_reference_date as _cer_reference_date  # noqa: F401  re-export usado por bond_detail
from core.domain.conventions import resolve_settle
from core.domain.models import MarketSnapshot
from core.domain.pricing import metrics
from core.domain.pricing.context import PricingContext
from core.domain.pricing.registry import strategy_for
from core.domain.pricing.stubs import ZeroTamar
from core.domain.pricing.tamar import tamar_dual_payoff_at
from core.domain.xirr import xirr as _xirr

_PCT_CHANGE_EPS = 1e-12


def _is_cer_type(instrument_type: str) -> bool:
    """Compat: `bond_detail` lo importa. Espejo de `Instrument.is_cer` sobre el
    string crudo (excluye variantes TAMAR; matchea CER/CON CUPON/STEP-UP)."""
    itype = instrument_type.upper().strip()
    if "TAMAR" in itype:
        return False
    return any(token in itype for token in ("CER", "CON CUPON", "STEP-UP"))


class FinancialEngine:
    # ------------------------------------------------------------------ #
    # Núcleo (delega al registry de strategies vía PricingContext).
    # ------------------------------------------------------------------ #
    @staticmethod
    def xirr(flows: List[float], dates: List[date]) -> float:
        return _xirr(flows, dates)

    @staticmethod
    def calculate_technical_value(snapshot: MarketSnapshot, indices_provider, fx_provider=None,
                                  ref_date: Optional[date] = None) -> float:
        inst = snapshot.instrument
        if not inst:
            return 100.0
        ref = ref_date if ref_date is not None else date.today()
        ctx = PricingContext(settle=ref, indices=indices_provider, fx=fx_provider)
        return strategy_for(inst).technical_value(inst, ctx)

    @staticmethod
    def calculate_tir(snapshot: MarketSnapshot, indices_provider=None, fx_provider=None,
                      settle_date: Optional[date] = None,
                      tamar_forecast: Optional[float] = None) -> Optional[float]:
        inst = snapshot.instrument
        if not inst or not snapshot.price:
            return None
        settle = resolve_settle(inst.instrument_type, settle_date)
        ctx = PricingContext(settle=settle, indices=indices_provider, fx=fx_provider,
                             tamar_forecast=tamar_forecast)
        return strategy_for(inst).tir(inst, snapshot.price, ctx)

    @staticmethod
    def calculate_duration(snapshot: MarketSnapshot, tir: float,
                           settle_date: Optional[date] = None) -> Optional[float]:
        inst = snapshot.instrument
        if not inst or tir is None or np.isnan(tir):
            return None
        settle = resolve_settle(inst.instrument_type, settle_date)
        ctx = PricingContext(settle=settle)
        return strategy_for(inst).duration(inst, tir, ctx)

    @staticmethod
    def price_from_tir(snapshot: MarketSnapshot, tir: float,
                       indices_provider=None, fx_provider=None,
                       settle_date: Optional[date] = None,
                       tamar_forecast: Optional[float] = None) -> Optional[float]:
        if snapshot is None or tir is None:
            return None
        inst = snapshot.instrument
        if inst is None:
            return None
        settle = resolve_settle(inst.instrument_type, settle_date)
        ctx = PricingContext(settle=settle, indices=indices_provider, fx=fx_provider,
                             tamar_forecast=tamar_forecast)
        return strategy_for(inst).price_from_tir(inst, tir, ctx)

    @staticmethod
    def tir_from_price(snapshot: MarketSnapshot, price_override: float,
                       indices_provider=None, fx_provider=None,
                       settle_date: Optional[date] = None,
                       tamar_forecast: Optional[float] = None) -> Optional[float]:
        """Inversa de calculate_tir: TIR para un precio (dirty) dado."""
        if snapshot is None or price_override is None or price_override <= 0:
            return None
        snap_override = snapshot.model_copy(update={"price": float(price_override)})
        return FinancialEngine.calculate_tir(snap_override, indices_provider, fx_provider,
                                             settle_date=settle_date, tamar_forecast=tamar_forecast)

    # ------------------------------------------------------------------ #
    # TAMAR helpers de la calculadora del popup.
    # ------------------------------------------------------------------ #
    @staticmethod
    def projected_payoff(instrument, indices_provider, tamar_forecast: Optional[float] = None,
                         ref_date: Optional[date] = None) -> Optional[float]:
        """Payback proyectado per-100 a vencimiento para bonos TAMAR-family."""
        if instrument is None or indices_provider is None:
            return None
        if not instrument.emission_date or not instrument.maturity_date:
            return None
        settle = ref_date if ref_date is not None else date.today()
        if instrument.maturity_date <= settle:
            return None
        return tamar_dual_payoff_at(instrument, settle, indices_provider,
                                    tamar_forecast=tamar_forecast, to_date=instrument.maturity_date)

    @staticmethod
    def recompute_as_tamar_puro(snapshot: MarketSnapshot, indices_provider):
        """Revalúa un DUAL como si fuera TAMAR PURO bullet (strip floor/CER)."""
        inst = snapshot.instrument
        if inst is None:
            return None, None, None
        inst_clone = inst.model_copy(update={"instrument_type": "PURO", "floor_rate_monthly": None})
        snap_clone = snapshot.model_copy(update={"instrument": inst_clone})
        tir = FinancialEngine.calculate_tir(snap_clone, indices_provider=indices_provider)
        vtec = FinancialEngine.calculate_technical_value(snap_clone, indices_provider=indices_provider)
        md = FinancialEngine.calculate_duration(snap_clone, tir) if tir is not None else None
        return tir, vtec, md

    @staticmethod
    def recompute_as_tasa_fija(snapshot: MarketSnapshot, indices_provider):
        """Revalúa un DUAL como si sólo corriera el floor fijo (TAMAR=0)."""
        inst = snapshot.instrument
        if inst is None or not inst.floor_rate_monthly:
            return None, None, None
        zero = ZeroTamar()
        tir = FinancialEngine.calculate_tir(snapshot, indices_provider=zero)
        vtec = FinancialEngine.calculate_technical_value(snapshot, indices_provider=zero)
        md = FinancialEngine.calculate_duration(snapshot, tir) if tir is not None else None
        return tir, vtec, md

    # ------------------------------------------------------------------ #
    # Precio teórico / variación.
    # ------------------------------------------------------------------ #
    @staticmethod
    def calculate_theoretical_price(instrument, tir: float, reference_date: date) -> Optional[float]:
        """Precio implícito de descontar flujos futuros al tir dado, con la
        convención de día-count declarada del instrumento."""
        if instrument is None or tir is None:
            return None
        future, yfs = metrics.discount_year_fractions(instrument, reference_date)
        if not future:
            return None
        try:
            price = 0.0
            for cf, years in zip(future, yfs):
                if years <= 0:
                    continue
                price += cf.total / (1 + tir) ** years
        except (OverflowError, ZeroDivisionError, ValueError):
            return None
        return price if price > 0 else None

    @staticmethod
    def calculate_pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
        if current is None or previous is None or abs(previous) < _PCT_CHANGE_EPS:
            return None
        return (current - previous) / previous

    # ------------------------------------------------------------------ #
    # Métricas a nivel instrumento (popup, tab 3) — delegan a pricing.metrics.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _period_bounds(instrument, ref_date: date) -> Optional[tuple]:
        return metrics.period_bounds(instrument, ref_date)

    @staticmethod
    def accrued_interest(instrument, ref_date: date) -> float:
        return metrics.accrued_interest(instrument, ref_date)

    @staticmethod
    def days_since_last_coupon(instrument, ref_date: date) -> Optional[int]:
        return metrics.days_since_last_coupon(instrument, ref_date)

    @staticmethod
    def residual_nominal(instrument, ref_date: date) -> float:
        return metrics.residual_nominal(instrument, ref_date)

    @staticmethod
    def current_yield(instrument, price_dirty: float, ref_date: date) -> Optional[float]:
        return metrics.current_yield(instrument, price_dirty, ref_date)

    @staticmethod
    def _vanilla_pv(instrument, tir: float, ref_date: date) -> Optional[float]:
        return metrics.vanilla_pv(instrument, tir, ref_date)

    @staticmethod
    def dv01(instrument, tir: float, ref_date: date) -> Optional[float]:
        return metrics.dv01(instrument, tir, ref_date)

    @staticmethod
    def convexity(instrument, tir: float, ref_date: date) -> Optional[float]:
        return metrics.convexity(instrument, tir, ref_date)

    # ------------------------------------------------------------------ #
    # Conversiones de tasa — delegan a conventions.
    # ------------------------------------------------------------------ #
    @staticmethod
    def tea_to_tem(tea: Optional[float]) -> Optional[float]:
        return conventions.tea_to_tem(tea)

    @staticmethod
    def tea_to_tna_monthly(tea: Optional[float]) -> Optional[float]:
        return conventions.tea_to_tna_monthly(tea)

    @staticmethod
    def tea_to_tna(tea: Optional[float]) -> Optional[float]:
        return conventions.tea_to_tna(tea)

    @staticmethod
    def tea_to_tna_freq(tea: Optional[float], freq: int) -> Optional[float]:
        return conventions.tea_to_tna_freq(tea, freq)

    @staticmethod
    def tea_to_tem_m12(tea: Optional[float]) -> Optional[float]:
        return conventions.tea_to_tem_m12(tea)
