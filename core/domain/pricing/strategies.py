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
| DualCerTamarStrategy | DUAL_CER_TAMAR       | payoff max-rieles; TIR cerrada; m=12   |
| DualDlTamarStrategy  | DUAL_DL_TAMAR        | payoff max(fx_base×TAMAR, 100×FX); TIR cerrada; m=12 |

El day-count 30/360 (BOPREAL y bonos CER marcados 30/360) NO es un tipo aparte:
en el `services.py` original es un chequeo inline (`is_30_360`) dentro del camino
general de TIR/duración. Por eso vive en `VanillaStrategy` y lo hereda Cer — un
BONCER 30/360 usa TIR real act/365.25 (rama CER) pero MacaulayD 30/360 (rama
duración), exactamente como el motor viejo.
"""

from __future__ import annotations

from datetime import date
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


def _fx_mayorista(ctx: PricingContext) -> Optional[float]:
    """Dólar del riel dólar-linked: mayorista venta VIVO (dolarapi, el mismo que usa
    `DolarLinkedStrategy`); si no responde, el fixing A3500 del BCRA al settle (forward-fill
    del provider); si tampoco, None. Nunca 0: un 0 de una fuente externa es dato ausente."""
    rate = _fx_offer(ctx.fx, "get_mayorista_venta")
    # `rate <= 0` cuenta como AUSENTE, no como respuesta: un 0 de dolarapi tiene que caer al
    # fixing igual que un None, si no el papel se queda sin riel DL por un dato basura.
    if (rate is None or rate <= 0) and ctx.indices is not None:
        fn = getattr(ctx.indices, "get_a3500", None)
        rate = fn(ctx.settle) if callable(fn) else None
    return rate if (rate is not None and rate > 0) else None


def dual_dl_tamar_payoff_at(inst, ref: date, ctx: PricingContext, *,
                            to_date: Optional[date] = None,
                            fx_rate: Optional[float] = None) -> Optional[float]:
    """Payoff de un DUAL_DL_TAMAR a `to_date` (default: vencimiento) en **pesos por 100 VN
    denominados en USD**: ``max(fx_base × riel TAMAR, 100 × FX)``.

    ESCALA (corrección de la revisión final, 2026-09-08): el papel cotiza en pesos por 100 VN
    **en USD**, como los dólar-linked, no por 100 VN en pesos como los duales TAMAR —
    Data912 2026-09-08: TMVE8 c=139.680, TZVD8 118.650, D31M7 147.800, contra TTS26 169,5 y
    TXMJ8 99,5; la ficha BYMA dice «moneda: Dólares» y «el Valor Nominal emitido convertido a
    Pesos». Por eso las DOS patas van en pesos por 100 VN USD: el riel DL son 100 USD al
    dólar del settle (`100 × FX`) y el riel TAMAR es la capitalización per-100 llevada a
    pesos por el TC inicial (`fx_base × T`).

    El riel TAMAR es `tamar_dual_payoff_at` TAL CUAL: para este tipo devuelve la
    capitalización mensual desde la emisión (sin floor —no es DUAL— y sin riel CER —no es
    DUAL_CER_TAMAR—), así que `tamar.py` y su contrato de cuatro pasos no se tocan. El riel
    DL usa el dólar del settle SIN proyectarlo (decisión de David, spec §1); `fx_rate`
    permite inyectarlo (popup/tests). Sin `fx_base`, sin dólar o sin serie TAMAR → None:
    no se inventa y no se precia con el riel TAMAR solo (un dual sin su segundo riel es un
    dato incompleto; la ABM exige `tc_inicial`)."""
    if not inst.fx_base or inst.fx_base <= 0:
        return None
    rate = fx_rate if fx_rate is not None else _fx_mayorista(ctx)
    if rate is None or rate <= 0:
        return None
    end = to_date if to_date is not None else inst.maturity_date
    riel_tamar = tamar_dual_payoff_at(inst, ref, ctx.indices,
                                      tamar_forecast=ctx.tamar_forecast, to_date=end)
    if riel_tamar is None:
        return None
    return max(riel_tamar * inst.fx_base, 100.0 * rate)


class DualCerTamarStrategy(VanillaStrategy):
    """DUAL CER/TAMAR (serie TXMJ*). Bullet que paga a vencimiento
    ``max(riel TAMAR, 100 × CER_vto/cer_base × (1+cer_spread)^años)`` —
    exactamente lo que computa `tamar.tamar_dual_payoff_at`.

    CONVENCIÓN: **TIR nominal (TEA)** contra ese payoff proyectado
    (`(payoff/precio)^(1/años) − 1`) y **V.Téc = max de rieles devengado al
    settle**, igual que TAMAR PURO/DUAL. `price_from_tir` (que ya descontaba el
    payoff nominal) queda como inversa exacta → el round-trip cierra por
    construcción.

    Por qué nominal y no real (había que unificar: los dos lados usaban unidades
    distintas):

    - Es la convención que `price_from_tir` YA implementaba y que documenta
      agents.md para este tipo ("payback / (1+tir)^t"). Elegir la pata real
      obligaba a re-derivar el precio deflactando por el CER **proyectado al
      vencimiento**, no por el del settle.
    - Deja la columna "TIR (TEA)" del panel DUAL/TAMAR comparable: los DUAL TAMAR
      que conviven en el mismo panel publican TEA nominal.
    - Es la que asume el docstring de `tamar.project_cer_at` ("su TIR queda
      acotada por el rail TAMAR") — sólo tiene sentido si la TIR sale del payoff
      de max-rieles.

    Lo que estaba mal antes: `tir` deflactaba el precio por CER_settle/cer_base y
    lo comparaba contra un redemption FIJO de 100 → ignoraba `inst.cer_spread`
    (la TIR salía idéntica con spread 0.00 y 0.04) y también el riel TAMAR;
    `technical_value` devolvía `100 × CER/cer_base` sin devengar el spread ni
    tomar el max de rieles; y el round-trip `tir → price_from_tir` devolvía ~1,95×
    el precio de entrada porque un lado era real y el otro nominal.
    """

    def technical_value(self, inst, ctx: PricingContext):
        ref = ctx.settle
        indices = ctx.indices
        if indices and inst.emission_date and inst.emission_date < ref:
            # `cer_settle_lag=ctx.settle_lag`: el ESCALÓN DE LIQUIDACIÓN T+N del
            # riel CER (paso 1 del contrato en `pricing/tamar.py`). `ref` acá es la
            # fecha de RUEDA (`calculate_technical_value` no la liquida), así que
            # sin esto el V.Téc se indexa por el CER de la rueda y no por el de la
            # liquidación — es lo que hacía la rama vieja vía
            # `cer_reference_date(settlement_byma_date(ref, settle_lag), cer_lag)`
            # y lo que sigue haciendo el fallback de abajo y todo `pricing/base.py`.
            v = tamar_dual_payoff_at(inst, ref, indices, to_date=ref,
                                     cer_settle_lag=ctx.settle_lag)
            if v is not None:
                return v
        # Fallback (bono aún no emitido / sin serie TAMAR utilizable): riel CER puro.
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
        if (indices and inst.emission_date
                and inst.maturity_date and inst.maturity_date > settle):
            payoff = tamar_dual_payoff_at(
                inst, settle, indices,
                tamar_forecast=ctx.tamar_forecast, to_date=inst.maturity_date,
            )
            if payoff is None or payoff <= 0:
                return None
            years = inst.year_fraction_to(inst.maturity_date, settle)
            if years <= 0 or price <= 0:
                return None
            try:
                return (payoff / price) ** (1.0 / years) - 1.0
            except (ValueError, OverflowError, ZeroDivisionError):
                return None
        if indices and inst.cer_base:
            return None   # guarda original: sin fecha utilizable, no se inventa TIR
        return super().tir(inst, price, ctx)

    def duration(self, inst, tir, ctx: PricingContext):
        """MD bullet con **m=12**, igual que TAMAR PURO/DUAL.

        agents.md › "Bonos TAMAR (PURO, DUAL, DUAL_CER_TAMAR)": *"MD bullet
        TAMAR/DUAL usa m=12 (capitalización mensual) → MD = years/(1+TEA)^(1/12).
        DL usa m=1"* — la excepción m=1 es Dólar Linked, no este tipo. Antes acá
        había un m=1 heredado de cuando la TIR era una tasa REAL de BONCER ZC;
        al unificar la TIR contra el payoff de max-rieles (TEA nominal, la misma
        que publican los DUAL TAMAR con los que comparte el panel `dual_tamar` y
        el eje X de la curva 'tamar') el m=1 quedó fuera de convención.

        Confirmación cruzada: `bond_detail._TAMAR_TYPES` ya incluye
        DUAL_CER_TAMAR, o sea que el popup viene publicando su "Tir Nominal" con
        m=12 (`tea_to_tna_monthly`) desde antes — la MD era la única pieza fuera
        de convención.

        Nota: el movimiento de MD que trajo esa unificación (TXMJ8: 1.8048 →
        1.2506 con m=1, 1.7646 con m=12) es consecuencia de la TIR, NO del lag
        CER — ver `tests/test_rem_R1_financiero_dual_md.py`.
        """
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


class DualDlTamarStrategy(VanillaStrategy):
    """DUAL dólar-linked/TAMAR (TMVE8). Bullet que paga a vencimiento
    ``max(fx_base × riel TAMAR capitalizado mensual desde la emisión, 100 × FX)``
    (`dual_dl_tamar_payoff_at`), en **pesos por 100 VN denominados en USD** — la escala en
    la que cotiza el papel (ver el docstring de la función: Data912 2026-09-08, 139.680).

    Misma convención que `DualCerTamarStrategy`: **TIR nominal (TEA)** contra el payoff
    proyectado, `(payoff / precio)^(1/años) − 1` (con el precio en esa misma escala);
    **V.Téc = max de rieles devengado al settle** (antes de la emisión, 100 USD al TC
    inicial = `100 × fx_base`); **MD bullet con m=12**; `price_from_tir` es la inversa exacta
    (round-trip por construcción). El riel DL toma el dólar del settle y no lo proyecta
    (spec 2026-09-08 §1). Sin `fx_base` o sin dólar NO precia (None). Vencido → camino
    general (`VanillaStrategy`)."""

    def technical_value(self, inst, ctx: PricingContext):
        ref = ctx.settle
        if not inst.emission_date or inst.emission_date >= ref:
            # Antes de la emisión no hay devengamiento: valen los 100 USD del VN al TC
            # inicial. Sin `fx_base` no hay escala en pesos que inventar → None (mismo
            # criterio que el payoff).
            return 100.0 * inst.fx_base if (inst.fx_base and inst.fx_base > 0) else None
        return dual_dl_tamar_payoff_at(inst, ref, ctx, to_date=ref)

    @staticmethod
    def _vivo(inst, ctx: PricingContext) -> bool:
        return bool(inst.emission_date and inst.maturity_date and inst.maturity_date > ctx.settle)

    def tir(self, inst, price, ctx: PricingContext):
        if not self._vivo(inst, ctx):
            return super().tir(inst, price, ctx)
        payoff = dual_dl_tamar_payoff_at(inst, ctx.settle, ctx)
        if payoff is None or payoff <= 0 or price is None or price <= 0:
            return None
        years = inst.year_fraction_to(inst.maturity_date, ctx.settle)
        if years <= 0:
            return None
        try:
            return (payoff / price) ** (1.0 / years) - 1.0
        except (ValueError, OverflowError, ZeroDivisionError):
            return None

    def duration(self, inst, tir, ctx: PricingContext):
        if tir is None or not np.isfinite(tir) or tir <= -1.0:
            return None
        if self._vivo(inst, ctx):
            years = inst.year_fraction_to(inst.maturity_date, ctx.settle)
            return years / (1 + tir) ** (1.0 / 12.0)
        return super().duration(inst, tir, ctx)

    def price_from_tir(self, inst, tir, ctx: PricingContext):
        if not self._vivo(inst, ctx):
            return super().price_from_tir(inst, tir, ctx)
        payoff = dual_dl_tamar_payoff_at(inst, ctx.settle, ctx)
        if payoff is None:
            return None
        years = inst.year_fraction_to(inst.maturity_date, ctx.settle)
        return payoff / (1 + tir) ** years
