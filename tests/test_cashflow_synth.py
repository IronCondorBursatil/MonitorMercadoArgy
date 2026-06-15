"""Tests del módulo `core.domain.cashflow_synth`.

El bug histórico que motivó estos tests: S29Y6 (LECAP) tenía TNA 33.88% en
el panel cuando Balanz oficial mostraba 21.09%. La causa era usar
`(vto - emision).days / 30` (días corridos) en vez de la convención AR
30/360. Estos tests previenen la regresión.
"""

from datetime import date

import pytest

from core.domain.cashflow_synth import (
    days_30_360,
    synth_cashflows,
)


# --------------------------------------------------------------------------- #
# 30/360 day-count
# --------------------------------------------------------------------------- #

class TestDays30360:
    def test_full_year_same_day(self):
        # 2025-05-30 → 2026-05-30 = exactamente 360 días en 30/360
        assert days_30_360(date(2025, 5, 30), date(2026, 5, 30)) == 360

    def test_s29y6_specific_case(self):
        # S29Y6: emisión 2025-05-30, vto 2026-05-29. Convención 30/360
        # da 359 → meses = 11.97 → payoff 132.04 (matchea Balanz).
        assert days_30_360(date(2025, 5, 30), date(2026, 5, 29)) == 359

    def test_isda_end_of_month_rule(self):
        # Regla ISDA: si end.day == 31 y start.day >= 30, end.day se trata como 30.
        assert days_30_360(date(2025, 5, 30), date(2025, 7, 31)) == 60  # mayo→julio, 2 meses

    def test_zero_days_same_date(self):
        d = date(2025, 5, 30)
        assert days_30_360(d, d) == 0


# --------------------------------------------------------------------------- #
# LECAP / BONCAP synth (el caso del bug)
# --------------------------------------------------------------------------- #

class TestLecapSynth:
    def test_s29y6_payoff_matches_balanz(self):
        """Ground truth: TEM 2.35%, emis 2025-05-30, vto 2026-05-29 →
        payoff esperado 132.0438 (NO 132.57 que daba el bug de días/30)."""
        row = {
            "clase": "LECAP",
            "tem_licit": 0.0235,
            "fecha_emision": date(2025, 5, 30),
            "fecha_pago": date(2026, 5, 29),
            "base calculo": "30/360",
        }
        cfs = synth_cashflows(row)
        assert len(cfs) == 1
        assert cfs[0].date == date(2026, 5, 29)
        assert cfs[0].interest == 0.0
        assert abs(cfs[0].amortization - 132.0438) < 0.001, (
            f"Payoff {cfs[0].amortization:.4f} doesn't match Balanz reference 132.04 "
            "— ¿regresó el bug de días/30?"
        )

    def test_s15s6_empty_base_defaults_to_30_360(self):
        """S15S6 (LECAP, TEM 1.99%, emis 2026-05-29, vto 2026-09-15) con base VACÍA →
        usa 30/360 (default) → payoff 107.21 (matchea Balanz). Cubre el alta sin base."""
        row = {
            "clase": "LECAP",
            "tem_licit": 0.0199,
            "fecha_emision": date(2026, 5, 29),
            "fecha_pago": date(2026, 9, 15),
            "base calculo": "",
        }
        cfs = synth_cashflows(row)
        assert len(cfs) == 1 and cfs[0].date == date(2026, 9, 15)
        assert abs(cfs[0].amortization - 107.21) < 0.01, cfs[0].amortization

    def test_actual_day_count_when_base_says_act(self):
        """Si base_calculo contiene 'act' (act/360, act/365), usar días corridos."""
        row = {
            "clase": "LECAP",
            "tem_licit": 0.0235,
            "fecha_emision": date(2025, 5, 30),
            "fecha_pago": date(2026, 5, 29),
            "base calculo": "Act/365",
        }
        cfs = synth_cashflows(row)
        # 364 días corridos / 30 = 12.13 meses → 132.57
        assert abs(cfs[0].amortization - 132.57) < 0.05

    def test_missing_tem_falls_back_to_100(self):
        """Sin tem_licit el LECAP devuelve 100 al vto (no crashea)."""
        row = {
            "clase": "LECAP",
            "fecha_emision": date(2025, 5, 30),
            "fecha_pago": date(2026, 5, 29),
        }
        cfs = synth_cashflows(row)
        assert len(cfs) == 1
        assert cfs[0].amortization == 100.0


# --------------------------------------------------------------------------- #
# Zero-coupon (LECER / BONCER ZC)
# --------------------------------------------------------------------------- #

class TestZeroCoupon:
    def test_lecer_pays_100_at_vto(self):
        row = {
            "tipo": "LECER",
            "fecha vencimiento": date(2026, 7, 31),
        }
        cfs = synth_cashflows(row)
        assert len(cfs) == 1
        assert cfs[0].date == date(2026, 7, 31)
        assert cfs[0].amortization == 100.0
        assert cfs[0].interest == 0.0

    def test_boncer_zc(self):
        row = {
            "tipo": "BONCER ZC",
            "fecha vencimiento": date(2027, 6, 30),
        }
        cfs = synth_cashflows(row)
        assert len(cfs) == 1
        assert cfs[0].amortization == 100.0


# --------------------------------------------------------------------------- #
# Bullet bond con cupón semestral
# --------------------------------------------------------------------------- #

class TestBulletCoupon:
    def test_bullet_regular_coupons_plus_amort_at_vto(self):
        """El synth ahora hace SOLO bullet con cupón regular desde la emisión (los
        amortizing / 1er cupón irregular van EXPLÍCITOS). BONCER 1.80% semestral,
        emis 2022-05-23, vto 2025-11-09 → cupones regulares + amort 100 al vto."""
        row = {
            "tipo": "BONCER",
            "fecha_emision": date(2022, 5, 23),
            "fecha_vencimiento": date(2025, 11, 9),
            "cupon anual %": 1.80,
            "frecuencia pagos": 2,
            "tipo amortizacion": "bullet",
        }
        cfs = synth_cashflows(row)
        # bullet: solo el vto amortiza, Σ = 100
        assert sum(cf.amortization for cf in cfs) == pytest.approx(100.0)
        assert all(cf.amortization == 0.0 for cf in cfs[:-1])
        last = cfs[-1]
        assert last.date == date(2025, 11, 9) and last.amortization == 100.0
        assert abs(last.interest - 0.9) < 0.01  # cupón regular 1.80%/2 × 100


# --------------------------------------------------------------------------- #
# Amortizing / 1er cupón irregular: el synth YA NO los hace — van EXPLÍCITOS
# (cashflow cargado desde la imagen o la hoja Cashflows del Excel). Antes vivían
# acá los tests de amort/prox_cupon; se retiraron al simplificar el synth a bullet.
# --------------------------------------------------------------------------- #

class TestAmortizingIsExplicitNow:
    def test_amort_fields_are_ignored_synth_is_bullet(self):
        """Aunque el row traiga campos de amort (legacy), el synth los IGNORA y produce
        bullet — los amortizing reales se cargan explícitos, no por params."""
        row = {
            "tipo": "BONAR", "fecha_emision": date(2024, 1, 9),
            "fecha_vencimiento": date(2030, 7, 9), "cupon anual %": 0.75,
            "frecuencia pagos": 2, "tipo amortizacion": "amortizing",
            "amort inicio": date(2024, 7, 9), "amort cantidad": 13,
        }
        cfs = synth_cashflows(row)
        assert sum(cf.amortization for cf in cfs) == pytest.approx(100.0)
        # bullet: 1 sola fila amortiza (al vto), no 13
        assert sum(1 for cf in cfs if cf.amortization > 0) == 1
        assert cfs[-1].date == date(2030, 7, 9) and cfs[-1].amortization == 100.0


# --------------------------------------------------------------------------- #
# Edge cases — robustez
# --------------------------------------------------------------------------- #

class TestEdgeCases:
    def test_vto_lte_emision_returns_empty(self):
        """Bond inválido (vto <= emisión) NO debe causar infinite loop —
        debe retornar lista vacía con warning. Esta es la guarda T2 del audit."""
        row = {
            "ticker": "BAD",
            "tipo": "BONAR",
            "fecha_emision": date(2025, 6, 1),
            "fecha_vencimiento": date(2025, 5, 1),  # ANTES de emisión
            "cupon anual %": 1.0,
        }
        cfs = synth_cashflows(row)
        assert cfs == []

    def test_vto_equal_emision_returns_empty(self):
        row = {
            "ticker": "EDGE",
            "tipo": "BONAR",
            "fecha_emision": date(2025, 5, 1),
            "fecha_vencimiento": date(2025, 5, 1),
            "cupon anual %": 1.0,
        }
        assert synth_cashflows(row) == []

    def test_missing_vto_returns_empty(self):
        assert synth_cashflows({"tipo": "BONAR", "cupon anual %": 1.0}) == []

    def test_unknown_type_without_coupon_returns_empty(self):
        row = {
            "tipo": "WHATEVER",
            "fecha_vencimiento": date(2026, 12, 31),
        }
        assert synth_cashflows(row) == []

    def test_dict_and_pandas_series_interchangeable(self):
        """El synth debe aceptar tanto dict como pd.Series (el repo le pasa
        Series, la ABM le pasa dict)."""
        import pandas as pd
        row_dict = {
            "tipo": "LECER",
            "fecha vencimiento": date(2026, 7, 31),
        }
        cfs_dict = synth_cashflows(row_dict)
        cfs_series = synth_cashflows(pd.Series(row_dict))
        assert len(cfs_dict) == len(cfs_series) == 1
        assert cfs_dict[0].date == cfs_series[0].date
        assert cfs_dict[0].amortization == cfs_series[0].amortization

    def test_datetime_input_normalized_to_date(self):
        """REGRESIÓN: openpyxl carga fechas del master como datetime.datetime
        (no date). El synth devolvía datetime sin convertir → el engine
        crasheaba con `cf.date >= settle_date` (TypeError mixed types).
        Verificamos explícitamente que cf.date sea date (no datetime)."""
        from datetime import datetime as _dt
        row = {
            "tipo": "LECER",
            "fecha vencimiento": _dt(2026, 7, 31, 0, 0, 0),  # datetime, no date
        }
        cfs = synth_cashflows(row)
        assert len(cfs) == 1
        assert type(cfs[0].date) is date, (
            f"Expected datetime.date, got {type(cfs[0].date).__name__}"
        )
        # Comparación con date pelado no debe explotar
        assert cfs[0].date >= date(2026, 1, 1)

    def test_pandas_timestamp_input_normalized_to_date(self):
        """REGRESIÓN: pandas también devuelve Timestamp (no date)."""
        import pandas as pd
        row = pd.Series({
            "tipo": "LECER",
            "fecha vencimiento": pd.Timestamp("2026-07-31"),
        })
        cfs = synth_cashflows(row)
        assert len(cfs) == 1
        assert type(cfs[0].date) is date
        assert cfs[0].date >= date(2026, 1, 1)


# --------------------------------------------------------------------------- #
# Step-up coupons (PARP, etc.)
# --------------------------------------------------------------------------- #

class TestStepUpCoupon:
    def test_step_up_picks_current_rate(self):
        """Step-up format: 'YYYY-MM-DD:rate;YYYY-MM-DD:rate' — debe usar la
        tasa activa a la fecha actual."""
        # Cupón sube al 1.50% desde 2024-12-31. Hoy es 2026 → debe usar 1.50%
        row = {
            "tipo": "BONAR",
            "fecha_emision": date(2020, 1, 1),
            "fecha_vencimiento": date(2030, 12, 31),
            "cupon anual %": "2020-01-01:0.50;2024-12-31:1.50",
            "frecuencia pagos": 2,
        }
        cfs = synth_cashflows(row)
        # No debe explotar y debe generar algo
        assert len(cfs) > 0
