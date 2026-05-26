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

from core.domain.conventions import cer_reference_date, days_30_360, settlement_byma
from core.domain.pricing import metrics
from core.domain.pricing.context import PricingContext
from core.domain.xirr import _JULIAN_YEAR, _xirr_from_years, xirr


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

        all_cfs = sorted(inst.cashflows or [], key=lambda cf: cf.date)
        past_cfs = [cf for cf in all_cfs if cf.date < ref]
        future_cfs = [cf for cf in all_cfs if cf.date >= ref]

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
                    settle = settlement_byma(ref.strftime("%Y-%m-%d"), lag=1).date()
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
            settle = settlement_byma(ref.strftime("%Y-%m-%d"), lag=1).date()
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
    # TIR — XIRR vanilla Act/365.25; rama 30/360 inline (BOPREAL y CER+30/360).
    # En el original `calculate_tir` chequea is_30_360 inline tras el camino
    # general, NO como tipo aparte — por eso vive acá (y CER lo hereda).
    # ------------------------------------------------------------------ #
    def tir(self, inst, price: float, ctx: PricingContext) -> Optional[float]:
        settle = ctx.settle
        future_cfs = inst.get_future_cashflows(settle)
        if not future_cfs:
            return None
        if inst.is_30_360:
            flows_arr = np.array([-price] + [cf.total for cf in future_cfs])
            years_arr = np.array(
                [0.0] + [days_30_360(settle, cf.date) / 360.0 for cf in future_cfs]
            )
            t = _xirr_from_years(flows_arr, years_arr)
            return float(t) if not np.isnan(t) else None
        flows = [-price] + [cf.total for cf in future_cfs]
        dates = [settle] + [cf.date for cf in future_cfs]
        t = xirr(flows, dates)
        return float(t) if not np.isnan(t) else None

    # ------------------------------------------------------------------ #
    # Modified Duration — Macaulay / (1+TEA)^(1/m), Act/365.25.
    # Rama 30/360 inline devuelve MacaulayD (convención BOPREAL/Balanz).
    # ------------------------------------------------------------------ #
    def duration(self, inst, tir: float, ctx: PricingContext) -> Optional[float]:
        settle = ctx.settle
        future_cfs = inst.get_future_cashflows(settle)
        if not future_cfs:
            return None
        if inst.is_30_360:
            total_pv, weighted_pv = 0.0, 0.0
            for cf in future_cfs:
                t = days_30_360(settle, cf.date) / 360.0
                pv = cf.total / (1 + tir) ** t
                total_pv += pv
                weighted_pv += pv * t
            return (weighted_pv / total_pv) if total_pv > 0 else None
        total_pv = 0.0
        weighted_pv = 0.0
        for cf in future_cfs:
            t = (cf.date - settle).days / _JULIAN_YEAR
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

    # ------------------------------------------------------------------ #
    # Precio desde TIR — PV vanilla.
    # ------------------------------------------------------------------ #
    def price_from_tir(self, inst, tir: float, ctx: PricingContext) -> Optional[float]:
        return metrics.vanilla_pv(inst, tir, ctx.settle)
