"""Anclas golden contra la calculadora de Balanz (verdad de mercado).

Estas ONs hard-dollar fueron verificadas a mano contra Balanz en la sesión de
alta. Son la prueba red→green del refactor de día-count: con el motor viejo
(descuento a 365.25) CICA daba 7.57%, Balanz 7.56%; al descontar con la
convención declarada (ACT/365) el motor matchea Balanz.

Se construyen vía `synth_cashflows` (mismo camino que la ABM) — deterministas,
sin depender de la DB ni del CSV. Las referencias (precio, settle, TIR, clean,
VT, accrued, MD) son las que muestra la calculadora de Balanz.
"""

from __future__ import annotations

from datetime import date

import pytest

from core.domain.cashflow_synth import synth_cashflows
from core.domain.models import Cashflow, Instrument
from core.domain.pricing import metrics
from core.domain.pricing.base import VanillaStrategy
from core.domain.pricing.context import PricingContext

_STRAT = VanillaStrategy()


def test_clisa_stub_final_period_matches_balanz():
    """CLISA 2031 (reestructurada, Ley NY/Extranjera): cupón step-up + amortización
    custom + **período final STUB** (vto 12/10/2031 pero el período regular cerraba
    el 10/12/2031). No es sintetizable → cashflows explícitos. Balanz @ precio 39.42
    dirty, settle 01/06/2026, 30/360: TIR 17.03%, MD 4.03, VT 56.92, accrued 1.06.
    Valida la extensión de stub (`metrics.discount_year_fractions`): SIN ella la TIR
    daría 17.47% (descuento a tiempo real del stub corto)."""
    rows = [
        (date(2026, 6, 10), 1.12, 0.0), (date(2026, 12, 10), 1.12, 0.0),
        (date(2027, 6, 10), 1.96, 0.0), (date(2027, 12, 10), 1.96, 0.0),
        (date(2028, 6, 10), 2.37, 0.0), (date(2028, 12, 10), 2.37, 0.0),
        (date(2029, 6, 10), 2.37, 0.0), (date(2029, 12, 10), 2.37, 0.0),
        (date(2030, 6, 10), 2.37, 0.0), (date(2030, 12, 10), 2.37, 0.0),
        (date(2031, 6, 10), 2.37, 0.0), (date(2031, 10, 12), 2.37, 55.86),
    ]
    inst = Instrument(
        ticker="CLSIO", short_name="CLISA - CLISA 2031", instrument_type="HARD DOLLAR",
        maturity_date=date(2031, 10, 12), emission_date=date(2021, 8, 17),
        payment_frequency=2, day_count="30/360",
        cashflows=tuple(Cashflow(date=d, interest=i, amortization=a) for d, i, a in rows),
    )
    settle, dirty = date(2026, 6, 1), 39.42
    ctx = PricingContext(settle=settle)
    tir = _STRAT.tir(inst, dirty, ctx)
    md = _STRAT.duration(inst, tir, ctx)
    ai = metrics.accrued_interest(inst, settle)
    vt = metrics.residual_nominal(inst, settle) + ai
    assert tir == pytest.approx(0.1703, abs=2e-4), f"TIR {tir*100:.2f}% (stub no extendido?)"
    assert tir < 0.172, "el stub final debe extenderse (sino daría 17.47%)"
    assert md == pytest.approx(4.03, abs=2e-2)
    assert vt == pytest.approx(56.92, abs=1e-2)
    assert ai == pytest.approx(1.06, abs=6e-3)   # 1.064 → Balanz muestra 1.06 (redondeo 2 dec)
    assert (dirty - ai) == pytest.approx(38.359, abs=1e-2)


def _build(cupon, freq, base, emis, vto, itype="HARD DOLLAR"):
    row = {
        "ticker": "GOLD", "tipo": itype,
        "fecha_emision": emis, "fecha_vencimiento": vto,
        "cupon anual %": cupon, "frecuencia pagos": freq,
        "base calculo": base, "tipo amortizacion": "bullet",
    }
    cfs = synth_cashflows(row)
    inst = Instrument(
        ticker="GOLD", short_name="GOLD", instrument_type=itype,
        maturity_date=vto, emission_date=emis, payment_frequency=freq,
        day_count=base, cashflows=tuple(cfs),
    )
    return inst, cfs


# (ticker, cupon, freq, base, emis, vto, settle, dirty, balanz_tir,
#  balanz_clean, balanz_vt, balanz_accrued, balanz_md)
_ANCHORS = [
    ("CICA", 8.0, 2, "ACT/365", date(2025, 12, 3), date(2028, 6, 3),
     date(2026, 6, 1), 105.0, 0.0756, 101.0548, 103.945, 3.9452, 1.76),
    ("CACB", 7.75, 4, "ACT/365", date(2025, 6, 17), date(2028, 6, 17),
     date(2026, 6, 1), 101.9, 0.0782, 100.2863, 101.6137, 1.6137, 1.85),
    ("BPCV", 3.25, 2, "ACT/365", date(2024, 11, 5), date(2027, 5, 5),
     date(2026, 6, 1), 98.23, 0.0558, 97.9896, 100.2404, 0.2404, 0.89),
]
_IDS = [a[0] for a in _ANCHORS]


@pytest.fixture(params=_ANCHORS, ids=_IDS)
def anchor(request):
    (tk, cupon, freq, base, emis, vto, settle, dirty,
     btir, bclean, bvt, bacc, bmd) = request.param
    inst, cfs = _build(cupon, freq, base, emis, vto)
    ctx = PricingContext(settle=settle)
    tir = _STRAT.tir(inst, dirty, ctx)
    return {
        "tk": tk, "inst": inst, "cfs": cfs, "settle": settle, "dirty": dirty,
        "tir": tir, "ctx": ctx,
        "balanz": dict(tir=btir, clean=bclean, vt=bvt, accrued=bacc, md=bmd),
    }


def test_tir_matches_balanz_within_1bp(anchor):
    # 1.5bp: cubre el redondeo de 2 decimales de Balanz + micro-diferencias de
    # método. Suficientemente ajustado para detectar el bug viejo de ~1bp+.
    assert anchor["tir"] == pytest.approx(anchor["balanz"]["tir"], abs=1.5e-4), anchor["tk"]


def test_clean_price_matches_balanz(anchor):
    ai = metrics.accrued_interest(anchor["inst"], anchor["settle"])
    clean = anchor["dirty"] - ai
    assert clean == pytest.approx(anchor["balanz"]["clean"], abs=1e-3), anchor["tk"]


def test_accrued_matches_balanz(anchor):
    ai = metrics.accrued_interest(anchor["inst"], anchor["settle"])
    assert ai == pytest.approx(anchor["balanz"]["accrued"], abs=2e-3), anchor["tk"]


def test_technical_value_matches_balanz(anchor):
    ai = metrics.accrued_interest(anchor["inst"], anchor["settle"])
    vt = metrics.residual_nominal(anchor["inst"], anchor["settle"]) + ai
    assert vt == pytest.approx(anchor["balanz"]["vt"], abs=1e-2), anchor["tk"]


def test_modified_duration_matches_balanz(anchor):
    md = _STRAT.duration(anchor["inst"], anchor["tir"], anchor["ctx"])
    assert md == pytest.approx(anchor["balanz"]["md"], abs=1e-2), anchor["tk"]


def test_cica_is_756_not_757_regression():
    """Prueba puntual del fix: CICA con ACT/365 da 7.56% (Balanz), NO el 7.57%
    que daba el motor viejo al descontar a 365.25. Si esto vuelve a 7.57, el
    descuento volvió a ignorar la convención declarada."""
    inst, _ = _build(8.0, 2, "ACT/365", date(2025, 12, 3), date(2028, 6, 3))
    tir = _STRAT.tir(inst, 105.0, PricingContext(settle=date(2026, 6, 1)))
    assert tir < 0.07565, f"TIR {tir*100:.3f}% — ¿volvió el descuento a 365.25?"
    assert tir == pytest.approx(0.0756, abs=8e-5)


def test_cica_at_36525_would_give_757():
    """Documenta el bug: con ACT/365.25 (forzado) CICA daría ~7.57% — el valor
    que NO debe producir el motor para una ON ACT/365."""
    inst, _ = _build(8.0, 2, "ACT/365.25", date(2025, 12, 3), date(2028, 6, 3))
    tir = _STRAT.tir(inst, 105.0, PricingContext(settle=date(2026, 6, 1)))
    assert tir > 0.0756, "365.25 debería dar TIR mayor (el viejo 7.57%)"


# --------------------------------------------------------------------------- #
# Golden de cashflows sintetizados (BACH 30/360, BF37 ACT/365) vs Balanz
# --------------------------------------------------------------------------- #

def test_bach_cashflows_all_4_pct_30_360():
    # Banco Macro Clase H: 8% 30/360 semestral → 10 cupones de 4.00 exactos.
    inst, cfs = _build(8.0, 2, "30/360", date(2026, 1, 28), date(2031, 1, 28))
    coupon_cfs = [cf for cf in cfs if cf.interest > 0]
    assert len(coupon_cfs) == 10
    for cf in coupon_cfs:
        assert cf.interest == pytest.approx(4.0, abs=1e-9), cf.date
    # bullet: amortización 100 sólo al vto
    assert sum(cf.amortization for cf in cfs) == pytest.approx(100.0)


def test_bf37_cashflows_act365_unequal():
    # Banco BBVA Clase 37: 6% ACT/365 semestral → cupones DESIGUALES por días
    # reales (184d=3.0247, 181d=2.9753), NO 3.00/3.00.
    inst, cfs = _build(6.0, 2, "ACT/365", date(2025, 8, 22), date(2026, 8, 22))
    ints = sorted(cf.interest for cf in cfs if cf.interest > 0)
    assert ints[0] == pytest.approx(2.9753, abs=2e-3)
    assert ints[-1] == pytest.approx(3.0247, abs=2e-3)
    assert ints[0] != pytest.approx(ints[-1], abs=1e-3)  # desiguales (ACT/365)
