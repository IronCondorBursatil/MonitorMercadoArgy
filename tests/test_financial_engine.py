"""Tests del `FinancialEngine` (XIRR, conversions TEA↔TEM↔TNA, MD).

Foco en regresión: TIR de un LECAP con cashflow sintético debe matchear lo
que vimos en Balanz para S29Y6 (TNA 21.09%).
"""

from datetime import date

import numpy as np

from core.domain.models import Cashflow, Instrument, MarketSnapshot
from core.domain.services import FinancialEngine


# --------------------------------------------------------------------------- #
# XIRR — Newton + Brentq solver
# --------------------------------------------------------------------------- #

class TestXIRR:
    def test_simple_one_year_bullet(self):
        """100 invertido hoy, 110 cobrado en 1 año → TIR ≈ 10%.
        XIRR usa act/365.25 (promedia leap years) → 365 días = 0.999 years
        → TIR resulta ligeramente arriba de 10% nominal."""
        today = date(2026, 1, 1)
        one_year = date(2027, 1, 1)
        tir = FinancialEngine.xirr([-100.0, 110.0], [today, one_year])
        assert 0.099 < tir < 0.101, f"TIR {tir} fuera de rango esperado"

    def test_zero_yield_when_no_gain(self):
        today = date(2026, 1, 1)
        one_year = date(2027, 1, 1)
        tir = FinancialEngine.xirr([-100.0, 100.0], [today, one_year])
        assert abs(tir) < 1e-6

    def test_negative_yield_when_loss(self):
        today = date(2026, 1, 1)
        one_year = date(2027, 1, 1)
        tir = FinancialEngine.xirr([-100.0, 95.0], [today, one_year])
        assert -0.052 < tir < -0.050

    def test_s29y6_tir_matches_balanz(self):
        """S29Y6 LECAP: price=131.21 hoy, payoff=132.04 al vto en 11 días.
        TIR esperada ≈ TNA 21.09% (Balanz oficial)."""
        today = date(2026, 5, 18)
        vto = date(2026, 5, 29)  # 11 días
        tir = FinancialEngine.xirr([-131.21, 132.04], [today, vto])
        # TIR como TEA (act/365.25 compound). TNA Balanz = 21.09%.
        # Conversion: TEA = (1 + TNA × days/365)^(365/days) - 1 con days=11
        # 21.09% TNA × 11/365 = 0.6357% en 11 días
        # TEA = 1.006357^(365/11) - 1 ≈ 0.2342 = 23.42%
        assert 0.22 < tir < 0.26, f"TIR {tir} no corresponde a TNA 21.09%"

    def test_empty_flows_returns_nan(self):
        assert np.isnan(FinancialEngine.xirr([], []))

    def test_single_flow_returns_nan(self):
        assert np.isnan(FinancialEngine.xirr([-100], [date(2026, 1, 1)]))


# --------------------------------------------------------------------------- #
# TEA ↔ TEM ↔ TNA conversions
# --------------------------------------------------------------------------- #

class TestRateConversions:
    def test_tea_to_tna_zero(self):
        assert abs(FinancialEngine.tea_to_tna(0.0)) < 1e-9

    def test_tea_to_tna_typical(self):
        # TEA 25% → TNA ≈ 22.31% (compounded daily)
        tna = FinancialEngine.tea_to_tna(0.25)
        assert 0.222 < tna < 0.224

    def test_tea_to_tem_typical(self):
        # TEA 25% → TEM ≈ 1.875% (30-day month, 365-day year)
        tem = FinancialEngine.tea_to_tem(0.25)
        assert 0.0185 < tem < 0.0190

    def test_tea_to_tna_inversion_consistency(self):
        """365 × ((1+TEA)^(1/365) − 1) → debe ser strictly less than TEA."""
        for tea in (0.05, 0.10, 0.25, 0.50, 1.00, 2.00):
            tna = FinancialEngine.tea_to_tna(tea)
            assert 0 < tna < tea

    def test_tea_to_tna_invalid_inputs(self):
        assert FinancialEngine.tea_to_tna(None) is None
        assert FinancialEngine.tea_to_tna(-1.0) is None  # boundary
        assert FinancialEngine.tea_to_tna(-2.0) is None  # below boundary

    def test_tea_to_tem_invalid_inputs(self):
        assert FinancialEngine.tea_to_tem(None) is None
        assert FinancialEngine.tea_to_tem(-1.0) is None


# --------------------------------------------------------------------------- #
# Modified Duration
# --------------------------------------------------------------------------- #

class TestModifiedDuration:
    def _make_zero_coupon_inst(self, vto_days: int) -> Instrument:
        """Bullet zero-coupon a vto_days desde hoy."""
        from datetime import timedelta
        today = date.today()
        vto = today + timedelta(days=vto_days)
        return Instrument(
            ticker="TEST", short_name="TEST", instrument_type="BONCER ZC",
            maturity_date=vto, emission_date=today - timedelta(days=30),
            payment_frequency=1, cer_base=1.0, cer_lag=10,
            cashflows=[Cashflow(date=vto, amortization=100.0, interest=0.0)],
        )

    def test_md_zero_for_already_matured(self):
        """Si todos los cashflows son pasados, MD debe ser None o 0."""
        from datetime import timedelta
        today = date.today()
        inst = Instrument(
            ticker="DEAD", short_name="DEAD", instrument_type="BONCER ZC",
            maturity_date=today - timedelta(days=10),
            emission_date=today - timedelta(days=365),
            payment_frequency=1, cer_base=1.0, cer_lag=10,
            cashflows=[Cashflow(date=today - timedelta(days=10),
                                amortization=100.0, interest=0.0)],
        )
        snap = MarketSnapshot(instrument=inst, price=99.0, last_update=today)
        md = FinancialEngine.calculate_duration(snap, tir=0.10)
        assert md is None or md == 0

    def test_md_increases_with_maturity(self):
        """Para zero-coupons, MD ≈ time-to-maturity (decreasing in TIR).
        Bullet más lejano → MD mayor."""
        inst_short = self._make_zero_coupon_inst(180)   # 6 meses
        inst_long = self._make_zero_coupon_inst(720)    # 2 años
        snap_short = MarketSnapshot(instrument=inst_short, price=95.0, last_update=date.today())
        snap_long = MarketSnapshot(instrument=inst_long, price=85.0, last_update=date.today())
        md_short = FinancialEngine.calculate_duration(snap_short, tir=0.10)
        md_long = FinancialEngine.calculate_duration(snap_long, tir=0.10)
        assert md_short is not None and md_long is not None
        assert md_short < md_long, f"MD short ({md_short}) debe ser < long ({md_long})"

    def test_md_returns_none_when_tir_is_nan(self):
        inst = self._make_zero_coupon_inst(180)
        snap = MarketSnapshot(instrument=inst, price=95.0, last_update=date.today())
        assert FinancialEngine.calculate_duration(snap, tir=None) is None
        assert FinancialEngine.calculate_duration(snap, tir=float("nan")) is None
