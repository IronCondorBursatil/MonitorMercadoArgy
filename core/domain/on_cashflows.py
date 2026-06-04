"""Generador de cashflows USD para Obligaciones Negociables (ONs hard-dollar).

A diferencia de `cashflow_synth._synth_coupon_bond`, el capital amortiza con su
PROPIA cadencia (`capital_freq`), independiente de la del cupón (`coupon_freq`):
las ONs corporativas suelen pagar cupón semestral pero amortizar capital anual.

Convención (alineada al glosario IAMC de renta fija):
  - Las fechas de cupón se anclan al VENCIMIENTO y retroceden por período.
  - El cupón de cada fecha se calcula sobre el saldo de capital vigente (declina
    a medida que amortiza).
  - Amortización bullet (1 cuota = VR al vto) o en `amort_count` cuotas iguales
    de capital, la última al vencimiento.
  - Cupón cero → único pago de VR al vencimiento.

Montos en base 100 de VN (el precio del ticker …D viene en USD base 100).
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from dateutil.relativedelta import relativedelta

from core.domain.models import Cashflow


def _coupon_dates(emission: date, maturity: date, months: int,
                  anchor: Optional[date] = None) -> List[date]:
    """Fechas de cupón en la grilla del `anchor` (el próximo cupón informado), o
    del vto por default. Cubre (emisión, vto] y siempre termina en el vto (stub si
    no alinea). El anclaje importa cuando el día de cupón ≠ día de vto: AERB paga
    el 23 (emisión) con stub al 15 del vto; otros pagan el día del vto con stub al
    inicio. Sin anchor → grilla del vto (caso alineado, la mayoría)."""
    a = anchor or maturity
    out: set[date] = set()
    d = a
    while d > emission:
        if d <= maturity:
            out.add(d)
        d -= relativedelta(months=months)
    d = a + relativedelta(months=months)
    while d < maturity:
        out.add(d)
        d += relativedelta(months=months)
    out.add(maturity)
    return sorted(out)


def amort_schedule(prox_cap: date, maturity: date, capital_freq: int,
                   cuotas: int) -> List[date]:
    """Fechas de las cuotas de capital REMANENTES, terminando en el vto.

    Para bullets (`cuotas` <= 1) → [vto]. Para amortizing, se generan desde el
    próximo pago de capital hacia adelante por la cadencia de capital hasta el
    vencimiento (inclusive) — así sólo se modelan las cuotas que faltan (el VR
    informado ya descuenta las ya pagadas)."""
    if cuotas is None or cuotas <= 1 or prox_cap is None or prox_cap >= maturity:
        return [maturity]
    months = max(12 // (capital_freq if capital_freq and capital_freq > 0 else 1), 1)
    out, d = [], prox_cap
    while d < maturity:
        out.append(d)
        d = d + relativedelta(months=months)
    out.append(maturity)
    return sorted(set(out))


def build_on_cashflows(
    emission: date,
    maturity: date,
    coupon_rate: float,
    coupon_freq: int,
    vr: float = 100.0,
    amort_dates: Optional[List[date]] = None,
    zero_coupon: bool = False,
    next_coupon: Optional[date] = None,
) -> List[Cashflow]:
    """Construye el schedule de cashflows de una ON.

    `coupon_rate` en decimal (0.078 = 7.8%); `coupon_freq` en pagos por año
    (1/2/4/12). `vr` es el valor residual base 100 (capital vivo); se reparte en
    partes iguales entre `amort_dates` (las cuotas remanentes, ver `amort_schedule`)
    o, si no se pasan, bullet al vencimiento. `next_coupon` (próximo cupón informado)
    fija la grilla de fechas de cupón; sin él se usa la grilla del vto.
    """
    if maturity is None or emission is None or maturity <= emission:
        return []

    if zero_coupon or not coupon_rate:
        return [Cashflow(date=maturity, amortization=vr, interest=0.0)]

    cfreq = coupon_freq if coupon_freq and coupon_freq > 0 else 1
    coupon_set = set(_coupon_dates(emission, maturity, max(12 // cfreq, 1), next_coupon))

    dates = sorted({d for d in (amort_dates or [maturity]) if d}) or [maturity]
    per = vr / len(dates)
    amort_map = {d: per for d in dates}

    cfs: List[Cashflow] = []
    outstanding = vr
    for d in sorted(coupon_set | set(amort_map)):
        interest = outstanding * coupon_rate / cfreq if d in coupon_set else 0.0
        amort = amort_map.get(d, 0.0)
        outstanding = max(outstanding - amort, 0.0)
        cfs.append(Cashflow(date=d, amortization=amort, interest=interest))
    return cfs
