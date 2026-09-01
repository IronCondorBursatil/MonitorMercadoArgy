"""Convenciones de display de rendimiento alineadas al informe IAMC.

Cubre las 3 mejoras detectadas al cargar las ONs hard-dollar (jun-2026), todas
DISPLAY-only (no tocan TIR efectiva / VT / MD / accrued money):
  1. Tir Nominal a la frecuencia del cupón (m=freq) — `tea_to_tna_freq` + selección por tipo.
  2. Current yield = cupón anual nominal / clean (no Σ próx-12m / dirty).
  3. Días corridos contados desde el día hábil de pago (mismo shift que el accrued).
"""

from datetime import date

from core.domain.models import Cashflow, Instrument
from core.domain.services import FinancialEngine


def _bond(cashflows, *, emis, vto, freq=2, itype="HARD DOLLAR", dc="ACT/365"):
    return Instrument(
        ticker="TEST", short_name="TEST", instrument_type=itype,
        emission_date=emis, maturity_date=vto, payment_frequency=freq,
        day_count=dc, cashflows=cashflows,
    )


# --------------------------------------------------------------------------- #
# 1. tea_to_tna_freq: nominal a la frecuencia del cupón (m=freq)
# --------------------------------------------------------------------------- #

class TestTeaToTnaFreq:
    def test_semiannual_matches_broker_formula(self):
        # 2 × ((1+TEA)^(1/2) − 1). TEA 5.5642% → 5.4889% (informe de referencia EMC1D 5.49).
        tna = FinancialEngine.tea_to_tna_freq(0.055642, 2)
        assert abs(tna - 2 * ((1.055642) ** 0.5 - 1)) < 1e-12
        assert 0.0548 < tna < 0.0550

    def test_quarterly_freq4(self):
        tna = FinancialEngine.tea_to_tna_freq(0.0782, 4)
        assert abs(tna - 4 * ((1.0782) ** 0.25 - 1)) < 1e-12

    def test_freq_nominal_above_daily_nominal(self):
        # m=freq (semestral) > m=365 (diario) > nada; el informe usa el de la frecuencia.
        tea = 0.0676
        assert FinancialEngine.tea_to_tna_freq(tea, 2) > FinancialEngine.tea_to_tna(tea)

    def test_invalid_inputs(self):
        assert FinancialEngine.tea_to_tna_freq(None, 2) is None
        assert FinancialEngine.tea_to_tna_freq(-1.0, 2) is None
        assert FinancialEngine.tea_to_tna_freq(0.05, 0) is None
        assert FinancialEngine.tea_to_tna_freq(0.05, None) is None


# --------------------------------------------------------------------------- #
# 2. current_yield = cupón anual nominal / clean
# --------------------------------------------------------------------------- #

class TestCurrentYield:
    def test_annual_coupon_over_clean(self):
        """CY = (cupón anual = next_cf.interest / dcf) / clean (NO Σ próx-12m / dirty)."""
        inst = _bond(
            [Cashflow(date=date(2026, 1, 5), amortization=0.0, interest=3.78),
             Cashflow(date=date(2026, 7, 4), amortization=100.0, interest=3.72)],
            emis=date(2025, 7, 4), vto=date(2026, 7, 4),
        )
        ref, price = date(2026, 6, 2), 105.15
        cy = FinancialEngine.current_yield(inst, price, ref)
        clean = price - FinancialEngine.accrued_interest(inst, ref)
        dcf = inst.year_fraction_to(date(2026, 7, 4), date(2026, 1, 5))  # período corriente
        assert cy is not None
        assert abs(cy - (3.72 / dcf) / clean) < 1e-9

    def test_subyear_bond_not_halved(self):
        """Bono tipo AFCID (2 cupones, último cupón ya pagado): la vieja fórmula
        (Σ próx-12m / dirty) capturaba medio cupón anual → CY ~a la mitad. La nueva
        recupera la tasa de cupón (≈6.5%) desde el cupón vigente → ~el doble."""
        inst = _bond(
            [Cashflow(date=date(2026, 5, 7), amortization=0.0, interest=3.22),
             Cashflow(date=date(2026, 11, 7), amortization=100.0, interest=3.28)],
            emis=date(2025, 11, 7), vto=date(2026, 11, 7),
        )
        ref, price = date(2026, 6, 2), 101.15
        cy = FinancialEngine.current_yield(inst, price, ref)
        old_style = 3.28 / price  # Σ próx-12m / dirty (sólo el cupón de nov en la ventana)
        assert cy is not None
        assert 0.063 < cy < 0.066          # ≈ 6.5% cupón / clean (informe de referencia 6.46%)
        assert cy > 1.8 * old_style

    def test_zero_coupon_returns_none(self):
        inst = _bond(
            [Cashflow(date=date(2027, 1, 1), amortization=100.0, interest=0.0)],
            emis=date(2026, 1, 1), vto=date(2027, 1, 1),
        )
        assert FinancialEngine.current_yield(inst, 95.0, date(2026, 6, 2)) is None


# --------------------------------------------------------------------------- #
# 3. days_since_last_coupon: contar desde el día hábil de pago
# --------------------------------------------------------------------------- #

class TestDaysSinceLastCoupon:
    def test_business_day_shift_weekend_coupon(self):
        """Cupón programado domingo 04-Ene-2026 → paga lunes 05-Ene → días desde el 05."""
        inst = _bond(
            [Cashflow(date=date(2026, 1, 4), amortization=0.0, interest=3.78),
             Cashflow(date=date(2026, 7, 4), amortization=100.0, interest=3.72)],
            emis=date(2025, 7, 4), vto=date(2026, 7, 4),
        )
        ref = date(2026, 6, 2)
        days = FinancialEngine.days_since_last_coupon(inst, ref)
        assert days == (ref - date(2026, 1, 5)).days  # 148, no 149


# --------------------------------------------------------------------------- #
# 4. Nominal mostrado: selección por tipo (m=freq vs m=12)
# --------------------------------------------------------------------------- #

class TestNominalSelection:
    def test_hard_dollar_uses_freq(self):
        from apps.web.bond_detail import _nominal_tna
        inst = _bond([Cashflow(date=date(2026, 7, 4), amortization=100.0, interest=3.72)],
                     emis=date(2025, 7, 4), vto=date(2026, 7, 4), freq=2)
        tea = 0.0556
        assert abs(_nominal_tna(inst, tea) - FinancialEngine.tea_to_tna_freq(tea, 2)) < 1e-12

    def test_tamar_uses_monthly(self):
        from apps.web.bond_detail import _nominal_tna
        inst = _bond([], emis=date(2025, 1, 1), vto=date(2027, 1, 1), itype="DUAL")
        tea = 0.30
        assert abs(_nominal_tna(inst, tea) - FinancialEngine.tea_to_tna_monthly(tea)) < 1e-12
