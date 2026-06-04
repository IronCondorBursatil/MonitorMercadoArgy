"""PricingStrategy (Protocol) + VanillaStrategy (camino general).

`VanillaStrategy` implementa el camino que en `services.py` venía DESPUÉS de los
early-returns por tipo (DL / TAMAR / DUAL_CER_TAMAR). Es decir: el cálculo
"general" de cada método, incluyendo el manejo inline de CER y del capitalizable
de un solo flujo (LECAP). Las strategies por familia (en `strategies.py`)
sobreescriben sólo su rama específica y delegan a `super()` cuando su guarda no
se cumple — reproduciendo EXACTAMENTE el fall-through del código original.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

import numpy as np

from core.domain.conventions import cer_reference_date, settlement_byma_date
from core.domain.pricing import metrics
from core.domain.pricing.context import PricingContext
from core.domain.xirr import _xirr_from_years


@runtime_checkable
class PricingStrategy(Protocol):
    def technical_value(self, inst, ctx: PricingContext) -> Optional[float]: ...
    def tir(self, inst, price: float, ctx: PricingContext) -> Optional[float]: ...
    def duration(self, inst, tir: float, ctx: PricingContext) -> Optional[float]: ...
    def price_from_tir(self, inst, tir: float, ctx: PricingContext) -> Optional[float]: ...


class VanillaStrategy:
    """Soberanos, LECAP/BONCAP/BONOFIJA y, vía herencia, el camino general que
    cubre CER (deflación inline en V.Téc) y los fall-throughs de los demás tipos."""

    # ------------------------------------------------------------------ #
    # V.Téc — branches "generales" de calculate_technical_value.
    # `ctx.settle` aquí es el `ref` de V.Téc (hoy o un override), NO el settle.
    # ------------------------------------------------------------------ #
    def technical_value(self, inst, ctx: PricingContext) -> Optional[float]:
        ref = ctx.settle
        indices = ctx.indices

        all_cfs = inst.cashflows  # ya cronológico (invariante del modelo)
        past_cfs = [cf for cf in all_cfs if cf.date <= ref]      # ex-cupón: flujo en
        future_cfs = [cf for cf in all_cfs if cf.date > ref]     # `ref` lo cobra el vendedor

        # Capitalizable de un solo flujo (LECAP/BONCAP): V.Téc crece
        # geométricamente de 100 (emisión) al payoff (vto).
        if (len(future_cfs) == 1 and not past_cfs
                and future_cfs[0].interest == 0
                and inst.emission_date and inst.emission_date < ref):
            payoff = future_cfs[0].amortization
            total = (future_cfs[0].date - inst.emission_date).days
            elapsed = (ref - inst.emission_date).days
            if total > 0 and payoff > 0 and 0 < elapsed <= total:
                base_value = 100.0 * (payoff / 100.0) ** (elapsed / total)
                if inst.is_cer and indices and inst.cer_base:
                    settle = settlement_byma_date(ref, lag=1)
                    target_date = cer_reference_date(settle, inst.cer_lag)
                    cer_val = indices.get_cer(target_date)
                    if cer_val:
                        return base_value * cer_val / inst.cer_base
                return base_value

        # Residual nominal en términos BASE (suma de amortizaciones futuras).
        residual = sum(cf.amortization for cf in future_cfs)
        if residual <= 0:
            amortized = sum(cf.amortization for cf in past_cfs)
            residual = max(100.0 - amortized, 0.0)

        accrued = metrics.accrued_interest(inst, ref)
        base_value = residual + accrued

        # Factor de indexación CER (NT N°8/2024 Eq. 13).
        if inst.is_cer and indices and inst.cer_base:
            settle = settlement_byma_date(ref, lag=1)
            target_date = cer_reference_date(settle, inst.cer_lag)
            cer_val = indices.get_cer(target_date)
            if cer_val:
                # Normalizar bonos con capital_factor > 1 (DICP/DIP0/CUAP) a base-100.
                total_amort_all = sum(cf.amortization for cf in all_cfs)
                if total_amort_all > 0.01:
                    base_value = base_value * 100.0 / total_amort_all
                return base_value * cer_val / inst.cer_base

        return base_value

    # ------------------------------------------------------------------ #
    # TIR — XIRR descontando con la convención declarada del instrumento
    # (`inst.year_fraction_to`): ACT/365 las ONs, ACT/365.25 soberanos, 30/360
    # BOPREAL/CER-30-360, ACT/ACT. Un único camino (sin split is_30_360); para
    # 30/360 y 365.25 es numéricamente idéntico a las fórmulas anteriores.
    # ------------------------------------------------------------------ #
    def tir(self, inst, price: float, ctx: PricingContext) -> Optional[float]:
        settle = ctx.settle
        future_cfs, yfs = metrics.discount_year_fractions(inst, settle)
        if not future_cfs:
            return None
        flows = np.array([-price] + [cf.total for cf in future_cfs])
        years = np.array([0.0] + yfs)
        t = _xirr_from_years(flows, years)
        return float(t) if not np.isnan(t) else None

    # ------------------------------------------------------------------ #
    # Modified Duration — Macaulay / (1+TEA)^(1/m), descontando con la convención
    # del instrumento. Un único camino. Guard de overflow: ante TIR degenerada
    # (ej. CUAP con CER mock → TIR absurda), `(1+tir)**t` puede desbordar → None
    # limpio en vez de OverflowError.
    # ------------------------------------------------------------------ #
    def duration(self, inst, tir: float, ctx: PricingContext) -> Optional[float]:
        settle = ctx.settle
        future_cfs, yfs = metrics.discount_year_fractions(inst, settle)
        if not future_cfs or tir is None or np.isnan(tir):
            return None
        try:
            total_pv = 0.0
            weighted_pv = 0.0
            for cf, t in zip(future_cfs, yfs):
                pv = cf.total / (1 + tir) ** t
                total_pv += pv
                weighted_pv += pv * t
            if total_pv <= 0:
                return None
            macaulay = weighted_pv / total_pv
            freq = getattr(inst, "payment_frequency", 1) or 1
            if len(future_cfs) <= 1:
                freq = 1
            return macaulay / (1 + tir) ** (1.0 / freq)
        except (OverflowError, ZeroDivisionError, ValueError):
            return None

    # ------------------------------------------------------------------ #
    # Precio desde TIR — PV vanilla.
    # ------------------------------------------------------------------ #
    def price_from_tir(self, inst, tir: float, ctx: PricingContext) -> Optional[float]:
        return metrics.vanilla_pv(inst, tir, ctx.settle)
