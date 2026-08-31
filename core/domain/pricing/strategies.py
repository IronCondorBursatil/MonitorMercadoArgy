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

from typing import Optional

import numpy as np

from core.domain.conventions import cer_reference_date, settlement_byma_date
from core.domain.pricing import metrics
from core.domain.pricing.base import VanillaStrategy
from core.domain.pricing.context import PricingContext
from core.domain.pricing.tamar import tamar_dual_payoff_at
from core.domain.xirr import _xirr_from_years


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
                # Descontar con la convención del bono + extensión de stub final
                # (discount_year_fractions); CER-30/360 usa 30/360.
                fut, yfs = metrics.discount_year_fractions(inst, settle)
                flows = np.array([-real_price] + [cf.total for cf in fut])
                years = np.array([0.0] + yfs)
                t = _xirr_from_years(flows, years)
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


def _fx_offer(fx, method: str) -> Optional[float]:
    """Lee un offer (venta) del provider de FX de forma tolerante: si el provider no
    expone ese método (mocks/providers viejos), devuelve None → la pata pesos no se
    convierte (degradación segura, igual que antes de agregar MEP/CCL)."""
    if fx is None:
        return None
    fn = getattr(fx, method, None)
    return fn() if callable(fn) else None


class DolarLinkedStrategy(VanillaStrategy):
    """USD multi-pata. Precio→USD por pata: la pata **pesos** (sufijo …O o mono-ticker
    tipo TZV*) se divide por el FX que devuelve `_peso_fx_rate`; **MEP** (…D) y **CABLE**
    (…C) ya cotizan en USD → cálculo normal (sin /FX). TIR/precio sobre los flujos USD
    **REALES** (maneja amortizables; NO asume bullet). Duración heredada de
    VanillaStrategy (Macaulay/(1+tir)^(1/m) sobre los flujos reales).

    Dólar-linked: paga pesos × FX oficial → la pata pesos se deflacta por el **oficial**
    (mayorista venta = A3500). `HardDollarStrategy` hereda todo y sólo cambia el FX de la
    pata pesos (MEP/CCL según ley)."""

    @staticmethod
    def _is_usd_leg(inst) -> bool:
        """MEP (…D) / CABLE (…C) cotizan ya en USD → no convertir. La pata pesos (…O)
        y los mono-ticker (TZV*) cotizan en pesos → /FX de `_peso_fx_rate`."""
        t = (inst.ticker or "").upper()
        return t.endswith("D") or t.endswith("C")

    def _peso_fx_rate(self, inst, ctx) -> Optional[float]:
        """FX (offer) para pasar la pata pesos → USD. Dólar-linked: oficial (A3500)."""
        return _fx_offer(ctx.fx, "get_mayorista_venta")

    def _usd_price(self, inst, price, ctx) -> Optional[float]:
        if self._is_usd_leg(inst):
            return price
        rate = self._peso_fx_rate(inst, ctx)
        return (price / rate) if (rate and rate > 0) else None

    def technical_value(self, inst, ctx: PricingContext):
        ref = ctx.settle
        residual_usd = sum(cf.amortization for cf in (inst.cashflows or []) if cf.date > ref)  # ex-cupón
        if residual_usd <= 0:
            residual_usd = 100.0
        # V.Téc = capital residual + intereses corridos (USD). El accrued es 0 para
        # los DL zero-coupon (CP28) pero ≠0 en hard-dollar con cupón (CP40 → 102.19).
        vt_usd = residual_usd + (metrics.accrued_interest(inst, ref) or 0.0)
        if self._is_usd_leg(inst):
            return vt_usd                             # MEP/CABLE → V.Téc en USD
        rate = self._peso_fx_rate(inst, ctx)
        if rate and rate > 0:
            return vt_usd * rate                      # pesos → V.Téc en pesos
        return super().technical_value(inst, ctx)

    def tir(self, inst, price, ctx: PricingContext):
        usd_price = self._usd_price(inst, price, ctx)
        if usd_price is None or usd_price <= 0:
            return None
        future_cfs, yfs = metrics.discount_year_fractions(inst, ctx.settle)
        if not future_cfs:
            return None
        flows = np.array([-usd_price] + [cf.total for cf in future_cfs])
        t = _xirr_from_years(flows, np.array([0.0] + yfs))
        return float(t) if not np.isnan(t) else None

    # duration: heredada de VanillaStrategy (flujos reales + m=freq). No depende del FX.

    def price_from_tir(self, inst, tir, ctx: PricingContext):
        pv_usd = metrics.vanilla_pv(inst, tir, ctx.settle)   # PV USD de los flujos reales
        if pv_usd is None:
            return None
        if self._is_usd_leg(inst):
            return pv_usd                             # MEP/CABLE → precio en USD
        rate = self._peso_fx_rate(inst, ctx)
        return (pv_usd * rate) if (rate and rate > 0) else None


class HardDollarStrategy(DolarLinkedStrategy):
    """ON hard-dollar (paga USD). Misma mecánica multi-pata que DolarLinked, pero la
    pata **pesos** (…O) NO se deflacta por el oficial: se pasa a USD por **MEP** (dólar
    bolsa) si el bono es **Ley Argentina**, o por **CCL/cable** (contadoconliqui) si es
    **Ley Extranjera** — o sin ley declarada (el universo ON es mayormente ley NY).
    Las patas …D (MEP) / …C (cable) ya cotizan en USD → directo (sin /FX)."""

    def _peso_fx_rate(self, inst, ctx) -> Optional[float]:
        if inst.is_ley_argentina:
            return _fx_offer(ctx.fx, "get_mep_venta")     # ley local → MEP (bolsa)
        return _fx_offer(ctx.fx, "get_ccl_venta")          # Extranjera / sin dato → CCL


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
            years = inst.year_fraction_to(inst.maturity_date, settle)
            if years <= 0 or price <= 0:
                return None
            try:
                return (expected_payback / price) ** (1.0 / years) - 1.0
            except (ValueError, OverflowError, ZeroDivisionError):
                return None
        return super().tir(inst, price, ctx)

    def duration(self, inst, tir, ctx: PricingContext):
        settle = ctx.settle
        if tir is None or not np.isfinite(tir) or tir <= -1.0:
            return None   # (1+tir)^(1/12) sería complejo con base ≤ 0
        if inst.maturity_date and inst.maturity_date > settle:
            years = inst.year_fraction_to(inst.maturity_date, settle)
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
            years = inst.year_fraction_to(inst.maturity_date, settle)
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
            settle = settlement_byma_date(ref, lag=ctx.settle_lag)
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
                    years = inst.year_fraction_to(inst.maturity_date, settle)
                    if years > 0 and real_price > 0:
                        try:
                            return (100.0 / real_price) ** (1.0 / years) - 1.0
                        except (ValueError, OverflowError, ZeroDivisionError):
                            pass
            return None
        return super().tir(inst, price, ctx)

    def duration(self, inst, tir, ctx: PricingContext):
        settle = ctx.settle
        if tir is None or not np.isfinite(tir) or tir <= -1.0:
            return None   # (1+tir) ≤ 0 → división por cero / duration sin sentido
        if inst.maturity_date and inst.maturity_date > settle:
            years = inst.year_fraction_to(inst.maturity_date, settle)
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
            years = inst.year_fraction_to(inst.maturity_date, settle)
            return payback / (1 + tir) ** years
        return super().price_from_tir(inst, tir, ctx)
