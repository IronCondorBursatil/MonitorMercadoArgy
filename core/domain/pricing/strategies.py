"""Pricing strategies por familia de bono.

Cada clase sobreescribe SÓLO la rama específica de su tipo (los early-returns
que en `services.py` precedían al cálculo general). Cuando su guarda no se
cumple, delega a `super()` → `VanillaStrategy` (camino general), reproduciendo
el fall-through del código original.

| Strategy             | Familia              | Especial vs vanilla                    |
|----------------------|----------------------|----------------------------------------|
| CerStrategy          | CER / LECER / BONCER | TIR real (deflactar por CER ratio)     |
| DolarLinkedStrategy  | DOLAR_LINKED         | V.Téc/precio en pesos; TIR en USD      |
| TamarStrategy        | PURO / DUAL          | payoff BONTE TAMAR; TIR cerrada; m=12  |
| DualCerTamarStrategy | DUAL_CER_TAMAR       | V.Téc/TIR CER ZC; precio vía payoff    |

El day-count 30/360 (BOPREAL y bonos CER marcados 30/360) NO es un tipo aparte:
en el `services.py` original es un chequeo inline (`is_30_360`) dentro del camino
general de TIR/duración. Por eso vive en `VanillaStrategy` y lo hereda Cer — un
BONCER 30/360 usa TIR real act/365.25 (rama CER) pero MacaulayD 30/360 (rama
duración), exactamente como el motor viejo.
"""

from __future__ import annotations

import numpy as np

from core.domain.conventions import cer_reference_date, settlement_byma
from core.domain.pricing import metrics
from core.domain.pricing.base import VanillaStrategy
from core.domain.pricing.context import PricingContext
from core.domain.pricing.tamar import tamar_dual_payoff_at
from core.domain.xirr import _JULIAN_YEAR, xirr


class CerStrategy(VanillaStrategy):
    """CER-ajustados. TIR real: deflactar price por CER_LIQ-10h / CER_BASE y
    resolver IRR contra los flujos nominales-base. V.Téc heredado (maneja el
    factor CER inline); duración heredada (vanilla)."""

    def tir(self, inst, price, ctx: PricingContext):
        indices = ctx.indices
        if indices and inst.cer_base:
            settle = ctx.settle
            future_cfs = inst.get_future_cashflows(settle)
            if not future_cfs:
                return None
            target_s = cer_reference_date(settle, inst.cer_lag)
            cer_s = indices.get_cer(target_s)
            if cer_s:
                real_price = price / (cer_s / inst.cer_base)
                flows = [-real_price] + [cf.total for cf in future_cfs]
                dates = [settle] + [cf.date for cf in future_cfs]
                t = xirr(flows, dates)
                return float(t) if not np.isnan(t) else None
        return super().tir(inst, price, ctx)

    def price_from_tir(self, inst, tir, ctx: PricingContext):
        indices = ctx.indices
        if indices and inst.cer_base:
            real_price = metrics.vanilla_pv(inst, tir, ctx.settle)
            if real_price is None:
                return None
            target_s = cer_reference_date(ctx.settle, inst.cer_lag)
            cer_s = indices.get_cer(target_s)
            if not cer_s:
                return real_price
            return real_price * (cer_s / inst.cer_base)
        return super().price_from_tir(inst, tir, ctx)


class DolarLinkedStrategy(VanillaStrategy):
    """Dólar-linked. V.Téc/precio en pesos = residual USD × FX mayorista;
    TIR en USD. Sin fx_provider, cae al camino vanilla."""

    def technical_value(self, inst, ctx: PricingContext):
        fx = ctx.fx
        if fx is not None:
            ref = ctx.settle
            fx_rate = fx.get_mayorista_venta()
            if fx_rate and fx_rate > 0:
                future_cfs = [cf for cf in (inst.cashflows or []) if cf.date >= ref]
                residual_usd = sum(cf.amortization for cf in future_cfs)
                if residual_usd <= 0:
                    residual_usd = 100.0
                return residual_usd * fx_rate
            return 100.0
        return super().technical_value(inst, ctx)

    def tir(self, inst, price, ctx: PricingContext):
        fx = ctx.fx
        if fx is not None:
            settle = ctx.settle
            fx_rate = fx.get_mayorista_venta()
            if fx_rate and fx_rate > 0 and inst.maturity_date and inst.maturity_date > settle:
                real_price_usd = price / fx_rate
                t = xirr([-real_price_usd, 100.0], [settle, inst.maturity_date])
                return float(t) if not np.isnan(t) else None
            return None
        return super().tir(inst, price, ctx)

    def duration(self, inst, tir, ctx: PricingContext):
        # Bullet anual (m=1). DL no depende de fx para la duración.
        settle = ctx.settle
        if inst.maturity_date and inst.maturity_date > settle:
            years = (inst.maturity_date - settle).days / _JULIAN_YEAR
            return years / (1 + tir) ** 1.0
        return super().duration(inst, tir, ctx)

    def price_from_tir(self, inst, tir, ctx: PricingContext):
        fx = ctx.fx
        if fx is not None:
            settle = ctx.settle
            fx_rate = fx.get_mayorista_venta()
            if not (fx_rate and fx_rate > 0 and inst.maturity_date and inst.maturity_date > settle):
                return None
            years = (inst.maturity_date - settle).days / _JULIAN_YEAR
            return 100.0 / (1 + tir) ** years * fx_rate
        return super().price_from_tir(inst, tir, ctx)


class TamarStrategy(VanillaStrategy):
    """TAMAR PURO / DUAL. Payback a vto = 100 × (1+TEM_max)^N_meses (fórmula
    oficial BONTE TAMAR, capitalización mensual). TIR cerrada (1 sólo flujo),
    duración bullet con m=12."""

    def technical_value(self, inst, ctx: PricingContext):
        ref = ctx.settle
        indices = ctx.indices
        if indices and inst.emission_date and inst.emission_date < ref:
            v = tamar_dual_payoff_at(inst, ref, indices, to_date=ref)
            if v is not None:
                return v
        return 100.0

    def tir(self, inst, price, ctx: PricingContext):
        indices = ctx.indices
        settle = ctx.settle
        if (indices and inst.emission_date
                and inst.maturity_date and inst.maturity_date > settle):
            expected_payback = tamar_dual_payoff_at(
                inst, settle, indices,
                tamar_forecast=ctx.tamar_forecast, to_date=inst.maturity_date,
            )
            if expected_payback is None or expected_payback <= 0:
                return None
            years = (inst.maturity_date - settle).days / _JULIAN_YEAR
            if years <= 0 or price <= 0:
                return None
            try:
                return (expected_payback / price) ** (1.0 / years) - 1.0
            except (ValueError, OverflowError, ZeroDivisionError):
                return None
        return super().tir(inst, price, ctx)

    def duration(self, inst, tir, ctx: PricingContext):
        settle = ctx.settle
        if inst.maturity_date and inst.maturity_date > settle:
            years = (inst.maturity_date - settle).days / _JULIAN_YEAR
            return years / (1 + tir) ** (1.0 / 12.0)
        return super().duration(inst, tir, ctx)

    def price_from_tir(self, inst, tir, ctx: PricingContext):
        indices = ctx.indices
        settle = ctx.settle
        if (indices and inst.emission_date
                and inst.maturity_date and inst.maturity_date > settle):
            payback = tamar_dual_payoff_at(
                inst, settle, indices,
                tamar_forecast=ctx.tamar_forecast, to_date=inst.maturity_date,
            )
            if payback is None:
                return None
            years = (inst.maturity_date - settle).days / _JULIAN_YEAR
            return payback / (1 + tir) ** years
        return super().price_from_tir(inst, tir, ctx)


class DualCerTamarStrategy(VanillaStrategy):
    """DUAL CER/TAMAR (TXMJ* series). V.Téc y TIR se computan como bono CER ZC
    (100 × CER/base; TIR real ZC); el precio_from_tir usa el payoff TAMAR
    (idéntico al agrupamiento del código original)."""

    def technical_value(self, inst, ctx: PricingContext):
        ref = ctx.settle
        indices = ctx.indices
        if indices and inst.cer_base:
            settle = settlement_byma(ref.strftime("%Y-%m-%d"), lag=1).date()
            target_date = cer_reference_date(settle, inst.cer_lag)
            cer_val = indices.get_cer(target_date)
            if cer_val:
                return 100.0 * cer_val / inst.cer_base
            return 100.0
        return super().technical_value(inst, ctx)

    def tir(self, inst, price, ctx: PricingContext):
        indices = ctx.indices
        settle = ctx.settle
        if indices and inst.cer_base:
            if inst.maturity_date and inst.maturity_date > settle:
                target_s = cer_reference_date(settle, inst.cer_lag)
                cer_s = indices.get_cer(target_s)
                if cer_s:
                    real_price = price / (cer_s / inst.cer_base)
                    years = (inst.maturity_date - settle).days / _JULIAN_YEAR
                    if years > 0 and real_price > 0:
                        try:
                            return (100.0 / real_price) ** (1.0 / years) - 1.0
                        except (ValueError, OverflowError, ZeroDivisionError):
                            pass
            return None
        return super().tir(inst, price, ctx)

    def duration(self, inst, tir, ctx: PricingContext):
        settle = ctx.settle
        if inst.maturity_date and inst.maturity_date > settle:
            years = (inst.maturity_date - settle).days / _JULIAN_YEAR
            return years / (1 + tir) ** 1.0
        return super().duration(inst, tir, ctx)

    def price_from_tir(self, inst, tir, ctx: PricingContext):
        indices = ctx.indices
        settle = ctx.settle
        if (indices and inst.emission_date
                and inst.maturity_date and inst.maturity_date > settle):
            payback = tamar_dual_payoff_at(
                inst, settle, indices,
                tamar_forecast=ctx.tamar_forecast, to_date=inst.maturity_date,
            )
            if payback is None:
                return None
            years = (inst.maturity_date - settle).days / _JULIAN_YEAR
            return payback / (1 + tir) ** years
        return super().price_from_tir(inst, tir, ctx)
