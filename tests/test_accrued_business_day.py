"""Intereses corridos desde la fecha de pago REAL (día hábil), no la programada.

Cuando el cupón anterior cae en fin de semana/feriado, se paga el día hábil
siguiente (convención following) y los días corridos se cuentan desde esa fecha
real — como hace la referencia. Caso testigo: Cresud XLIV (CS44) — cupón programado
17/01/2026 (sábado) pagado el 19/01/2026 → 133 días al settle 01/06/2026 (no 135).
Solo afecta accrued/V.Téc/clean/paridad; NO la TIR ni el precio.
"""

from datetime import date

import pytest

from apps.web.instruments_abm import _normalize_fields, _safe_synth
from core.domain.pricing import metrics
from core.infrastructure.repositories import build_instrument

SETTLE = date(2026, 6, 1)


def _cs44():
    fields = {
        "short_name": "Cresud", "tipo": "HARD DOLLAR",
        "fecha_emision": "2024-01-17", "fecha_vencimiento": "2027-01-17",
        "cupon anual %": "6", "frecuencia pagos": 2, "base calculo": "ACT/365",
        "tipo amortizacion": "bullet", "ticker_ars": "CS44D",
    }
    norm = _normalize_fields(fields)
    return build_instrument(norm, "Obligaciones_Negociables", _safe_synth(norm))


def test_accrued_counts_from_business_day_payment_date():
    """6% ACT/365, cupón 17/01/2026 (sáb) → pago 19/01/2026 (lun). Días corridos al
    01/06/2026 = 133 → accrued = 6%×133/365 = 2.186 (NO 135 días = 2.219)."""
    inst = _cs44()
    acc = metrics.accrued_interest(inst, SETTLE)
    assert acc == pytest.approx(6.0 / 100 * 133 / 365 * 100, abs=5e-3)   # 2.186
    assert acc == pytest.approx(2.186, abs=5e-3)
    # Lo viejo (135 días desde la fecha programada 17/01) daba ~2.219 → debe diferir.
    assert abs(acc - 2.219) > 0.02


def test_vt_and_parity_match_referencia_with_bday_accrued():
    """Con el accrued corregido, V.Téc = 102.19 y paridad = 101.19% (precio 103.4)."""
    inst = _cs44()
    acc = metrics.accrued_interest(inst, SETTLE)
    vt = 100.0 + acc                         # bullet → residual 100 + accrued
    assert vt == pytest.approx(102.19, abs=0.02)
    assert 103.4 / vt * 100 == pytest.approx(101.19, abs=0.05)


def test_weekday_coupon_unaffected():
    """Si el cupón anterior cae en día hábil, el accrued NO cambia (sin corrimiento).
    Cupón 17/07 (jueves/viernes hábiles) → cuenta normal."""
    inst = _cs44()
    # settle justo después del cupón hábil 17/07/2025 (jueves)
    settle = date(2025, 8, 1)
    acc = metrics.accrued_interest(inst, settle)
    # 17/07/2025 es hábil → elapsed = 15 días reales → 6%×15/365
    assert acc == pytest.approx(6.0 / 100 * 15 / 365 * 100, abs=5e-3)
