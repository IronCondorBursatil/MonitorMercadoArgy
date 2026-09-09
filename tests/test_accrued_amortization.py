"""ACT: tasa diaria sobre el capital vigente, aun con historia recortada.

Casos que motivaron el fix, relevados el 2026-09-08: Rizobacter XI amortiza
20/100 el 28/06/2026 (following 29/06), a 9% ACT/365; RZ8BO llega con historia
desde el cupón 03/09/2026, devengado desde 03/03 sobre VR60, aunque emitió en 2023.
El fixture XI reduce los pagos futuros a uno: conserva capital y cupón siguiente.
Procedencia del primer servicio XI A (ISIN AR0900508240): CNV,
https://aif2.cnv.gov.ar/presentations/publicview/bed1c3db-6892-4923-9a52-dc4e28f183f4
Captura archivada durante el alta en research-complejas-sources; sus términos
también quedan en data/imports/on-2026-09-08.json. Los esperados se calculan por
tasa contractual y capital, no por la implementación de accrued.
ZPC5 usa el primer período contractual 26/06/2026-26/03/2027 (6,5% ACT/365),
con fecha de emisión e inicio explícitas; las fuentes están en el mismo manifiesto.
"""

from datetime import date

import pytest

from core.domain.models import Cashflow, Instrument
from core.domain.pricing import metrics
from core.domain.pricing.context import PricingContext
from core.domain.pricing.strategies import DolarLinkedStrategy, HardDollarStrategy


@pytest.mark.parametrize("instrument_type,strategy", [
    ("HARD DOLLAR", HardDollarStrategy()),
    ("DOLLAR LINKED", DolarLinkedStrategy()),
])
def test_act_accrued_and_vt_use_post_amortization_capital(instrument_type, strategy):
    """No devengar sobre VN100 tras amortizar20; following no suma un día extra."""
    inst = Instrument(
        ticker="TESTD", short_name="Rizo XI fixture", instrument_type=instrument_type,
        emission_date=date(2026, 6, 5), maturity_date=date(2027, 3, 3),
        payment_frequency=2, day_count="ACT/365",
        cashflows=(
            Cashflow(date(2026, 6, 28), interest=100 * .09 * 23 / 365, amortization=20),
            Cashflow(date(2027, 3, 3), interest=80 * .09 * 248 / 365, amortization=80),
        ),
    )
    settle = date(2026, 9, 9)
    expected = 80 * .09 * 72 / 365  # 29/06 -> 09/09: 72 días
    assert metrics.accrued_interest(inst, settle) == pytest.approx(expected, rel=1e-12)
    assert strategy.technical_value(inst, PricingContext(settle=settle)) == pytest.approx(
        81.42027397260274, rel=1e-12,
    )
    assert metrics.accrued_interest(inst, date(2026, 6, 29)) == 0.0


@pytest.mark.parametrize("split_amortization", [False, True])
def test_act_amortization_scales_known_residual_not_assumed_original_100(split_amortization):
    """Historia comienza en VR60; amortizar15 deja45 (también con dos filas un día)."""
    past = [Cashflow(date(2026, 9, 3), interest=60 * .09 * 184 / 365,
                     amortization=0 if split_amortization else 15)]
    if split_amortization:
        past.append(Cashflow(date(2026, 9, 3), amortization=15))
    inst = Instrument(
        ticker="PARTD", short_name="VR60 fixture", instrument_type="HARD DOLLAR",
        emission_date=date(2026, 3, 3), maturity_date=date(2027, 3, 3),
        payment_frequency=2, day_count="ACT/365",
        cashflows=(*past, Cashflow(date(2027, 3, 3), interest=45 * .09 * 181 / 365,
                                  amortization=45)),
    )
    assert metrics.accrued_interest(inst, date(2026, 9, 9)) == pytest.approx(
        45 * .09 * 6 / 365, rel=1e-12,
    )


@pytest.mark.parametrize("frequency,previous_days", [(2, 184), (4, 92)])
def test_first_past_coupon_with_truncated_history_uses_regular_period(frequency, previous_days):
    """El cupón pasado NO devengó desde la emisión 2023; no diluir su tasa diaria."""
    inst = Instrument(
        ticker="RZ8BD", short_name="RZ8BO truncated fixture", instrument_type="DOLLAR LINKED",
        emission_date=date(2023, 2, 10), maturity_date=date(2027, 3, 3),
        payment_frequency=frequency, day_count="ACT/365",
        cashflows=(
            Cashflow(date(2026, 9, 3), interest=60 * .09 * previous_days / 365),
            Cashflow(date(2027, 3, 3), interest=60 * .09 * 181 / 365, amortization=60),
        ),
    )
    settle = date(2026, 9, 9)
    assert metrics.accrued_interest(inst, settle) == pytest.approx(
        0.08876712328767123, rel=1e-12,
    )
    assert DolarLinkedStrategy().technical_value(inst, PricingContext(settle=settle)) == pytest.approx(
        60.08876712328767, rel=1e-12,
    )
    assert inst.emission_date == date(2023, 2, 10)


@pytest.mark.parametrize("amortization", [0, 20])
def test_long_first_and_long_last_coupon_preserve_past_daily_rate(amortization):
    """Primer cupón9 meses real; último paga6 meses en9: NO diluir por el próximo."""
    residual = 100 - amortization
    inst = Instrument(
        ticker="LONGD", short_name="Long coupon fixture", instrument_type="HARD DOLLAR",
        emission_date=date(2025, 1, 2), maturity_date=date(2026, 7, 2),
        payment_frequency=2, day_count="ACT/365",
        cashflows=(
            Cashflow(date(2025, 10, 2), interest=100 * .09 * 273 / 365,
                     amortization=amortization),
            Cashflow(date(2026, 7, 2), interest=residual * .09 * 182 / 365,
                     amortization=residual),
        ),
    )
    assert metrics.accrued_interest(inst, date(2026, 1, 2)) == pytest.approx(
        residual * .09 * 92 / 365, rel=1e-12,
    )


def _explicit_accrual_instrument(loader, accrual_start="2026-06-26"):
    """Ambos read-paths reales, sin DB ni fixture compartida con el motor."""
    from core.infrastructure.repositories import build_instrument
    from core.infrastructure.db.catalog_repository import _orm_to_domain
    from core.infrastructure.db.models import CashflowORM, InstrumentORM

    fields = {
        "ticker_ars": "LONGD", "short_name": "ZPC5 long first coupon fixture",
        "tipo": "HARD DOLLAR", "fecha_emision": "2026-06-26",
        "fecha_vencimiento": "2027-06-26", "frecuencia pagos": 4,
        "base calculo": "ACT/365", "fecha_inicio_devengamiento": accrual_start,
    }
    flows = (
        Cashflow(date(2027, 3, 26), interest=100 * .065 * 273 / 365),
        Cashflow(date(2027, 6, 26), interest=100 * .065 * 92 / 365, amortization=100),
    )
    if loader == "build":
        return build_instrument(fields, "Obligaciones_Negociables", list(flows))
    orm = InstrumentORM(
        ticker="LONGD", short_name=fields["short_name"], instrument_type="HARD DOLLAR",
        emission_date=date(2026, 6, 26), maturity_date=date(2027, 6, 26),
        payment_frequency=4, cer_lag=10, day_count="ACT/365", raw_fields=fields,
        cashflows=[CashflowORM(fecha_pago=c.date, cupon_interes=c.interest,
                               amortizacion=c.amortization, es_ancla=False) for c in flows],
    )
    return _orm_to_domain(orm)


@pytest.mark.parametrize("loader", ["build", "orm"])
@pytest.mark.parametrize("settle,expected", [
    (date(2026, 9, 8), 100 * .065 * 74 / 365),
    (date(2027, 1, 8), 100 * .065 * 196 / 365),
    # Primer cupón 26/03/2027 = Viernes Santo -> pago lunes29/03, luego3 días.
    (date(2027, 4, 1), 100 * .065 * 3 / 365),
])
def test_explicit_accrual_start_preserves_first_nine_month_coupon(loader, settle, expected):
    """ZPC5: el inicio contractual vence al supuesto de dos períodos, también en enero."""
    inst = _explicit_accrual_instrument(loader)
    assert metrics.accrued_interest(inst, settle) == pytest.approx(expected, rel=1e-12)
    assert inst.emission_date == date(2026, 6, 26)
    assert inst.payment_frequency == 4


def test_explicit_accrual_start_for_first_past_coupon_overrides_regular_inference():
    """Un stub explícito recortado tampoco es un período semestral regular."""
    inst = Instrument(
        ticker="STUBD", short_name="Known truncated stub", instrument_type="HARD DOLLAR",
        emission_date=date(2023, 2, 10), accrual_start_date=date(2026, 5, 3),
        maturity_date=date(2027, 3, 3), payment_frequency=2, day_count="ACT/365",
        cashflows=(
            Cashflow(date(2026, 9, 3), interest=60 * .09 * 123 / 365),
            Cashflow(date(2027, 3, 3), interest=60 * .09 * 181 / 365, amortization=60),
        ),
    )
    assert metrics.accrued_interest(inst, date(2026, 9, 9)) == pytest.approx(
        60 * .09 * 6 / 365, rel=1e-12,
    )


def test_accrual_start_is_part_of_memo_key_for_a_shared_memo_clone():
    """Defensa del memo: modificar sólo el inicio no debe heredar el accrued previo."""
    from copy import copy

    inst = _explicit_accrual_instrument("build")
    settle = date(2027, 1, 8)
    metrics.accrued_interest(inst, settle)
    clone = copy(inst)  # comparte el memo, a diferencia de Instrument.model_copy.
    object.__setattr__(clone, "accrual_start_date", date(2026, 7, 1))
    # Cupón total explícito igual; período ahora268 días, de los que191 transcurrieron.
    assert metrics.accrued_interest(clone, settle) == pytest.approx(
        (100 * .065 * 273 / 365) * 191 / 268, rel=1e-12,
    )
