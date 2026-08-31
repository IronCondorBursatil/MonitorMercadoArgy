"""Guards numéricos de las métricas ante TIR patológica: convexity y duration deben
devolver None (no lanzar 500, no devolver un número COMPLEJO que rompa el template).

- convexity con TIR gigante → OverflowError si no hay guard (panel ON hace 500).
- duration con TIR ≤ -1.0 → (1+tir) ≤ 0 elevado a exponente fraccional da un complejo
  en Python (no lanza) que llega al template y explota con TypeError.
"""

from datetime import date

from dateutil.relativedelta import relativedelta

from core.domain.models import Cashflow, Instrument
from core.domain.pricing import metrics
from core.domain.pricing.base import VanillaStrategy
from core.domain.pricing.context import PricingContext
from core.domain.pricing.strategies import TamarStrategy

SETTLE = date(2026, 6, 1)


def _bond():
    maturity = SETTLE + relativedelta(years=5)
    cfs = []
    d = maturity
    while d > SETTLE:
        amort = 100.0 if d == maturity else 0.0
        cfs.append(Cashflow(date=d, amortization=amort, interest=5.0))
        d = d - relativedelta(months=6)
    return Instrument(
        ticker="P", short_name="P", instrument_type="BONAR",
        maturity_date=maturity, emission_date=SETTLE - relativedelta(years=2),
        payment_frequency=2, day_count="ACT/365", cashflows=tuple(sorted(cfs, key=lambda c: c.date)),
    )


def test_convexity_survives_overflow_tir():
    # TIR gigante → (1+tir)^(t+2) desborda el float → OverflowError sin el try/except
    # (el panel ON lo subía como 500). Con el guard: None.
    assert metrics.convexity(_bond(), 1e300, SETTLE) is None


def test_convexity_returns_none_below_minus_one():
    # (1+tir) ≤ 0 no tiene sentido de descuento → None (guard tir<=-1.0).
    assert metrics.convexity(_bond(), -1.5, SETTLE) is None
    assert metrics.convexity(_bond(), -1.0, SETTLE) is None


def test_vanilla_duration_never_complex():
    dur = VanillaStrategy().duration(_bond(), -2.0, PricingContext(settle=SETTLE))
    assert dur is None or isinstance(dur, float)


def test_tamar_duration_never_complex():
    # El caso del informe: TamarStrategy.duration(inst, -2.0) daba (0.80-0.21j).
    dur = TamarStrategy().duration(_bond(), -2.0, PricingContext(settle=SETTLE))
    assert dur is None or isinstance(dur, float)


def test_duration_valid_tir_still_positive():
    # No romper el camino normal: con TIR sana la duración sigue siendo un float > 0.
    dur = VanillaStrategy().duration(_bond(), 0.30, PricingContext(settle=SETTLE))
    assert isinstance(dur, float) and dur > 0
