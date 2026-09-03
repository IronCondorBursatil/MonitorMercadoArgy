"""Auditoría lote B — `build_on_cashflows` pagaba un cupón COMPLETO en el stub.

`_coupon_dates` agrega el vencimiento a la grilla aunque no caiga en ella, y el
interés se liquidaba como `coupon_rate / cfreq` para TODA fecha de la grilla, sin
prorratear por los días reales del período. Caso vivo: AERBO (AA2000, emisión
23-Dec-24, próx. cupón 23-Jun-26, vto 15-Dec-26, 5,5% semestral) → último período
de 175 días cobrando 2,75 en vez de 5,5 × 175/365 = 2,6370 (+42 bp de TIR).

Convención de prorrateo: ACT/365, la misma de `scripts/fix_on_plc2_schedule.py`
para el mismo patrón en PLC2 y la declarada para las ONs del informe.
"""
from __future__ import annotations

from datetime import date

import pytest

from core.domain.on_cashflows import build_on_cashflows


def _aerbo():
    return build_on_cashflows(
        emission=date(2024, 12, 23), maturity=date(2026, 12, 15),
        coupon_rate=0.055, coupon_freq=2, vr=100.0,
        amort_dates=[date(2026, 12, 15)], next_coupon=date(2026, 6, 23),
    )


def test_aerbo_prorratea_el_stub_final():
    cfs = _aerbo()
    fechas = [cf.date for cf in cfs]
    assert fechas[-1] == date(2026, 12, 15)
    ult = cfs[-1]
    esperado = 0.055 * (date(2026, 12, 15) - date(2026, 6, 23)).days / 365.0 * 100.0
    assert esperado == pytest.approx(2.63699, abs=1e-4)
    assert ult.interest == pytest.approx(esperado, abs=1e-9)
    assert ult.amortization == pytest.approx(100.0)


def test_aerbo_conserva_los_cupones_regulares_completos():
    """Sólo el período corto se proratea: los semestrales siguen pagando 2,75."""
    cfs = _aerbo()
    regulares = [cf for cf in cfs if cf.date != date(2026, 12, 15)]
    assert [cf.date for cf in regulares] == [
        date(2025, 6, 23), date(2025, 12, 23), date(2026, 6, 23)]
    for cf in regulares:
        assert cf.interest == pytest.approx(2.75)


def test_primer_periodo_corto_tambien_se_proratea():
    """YPF 2030 (emisión 28-Ago-25, primer cupón 22-Ene-26 = 147 días): el primer
    cupón NO es un semestre completo."""
    cfs = build_on_cashflows(emission=date(2025, 8, 28), maturity=date(2030, 7, 22),
                             coupon_rate=0.08, coupon_freq=2)
    primero = cfs[0]
    assert primero.date == date(2026, 1, 22)
    assert primero.interest == pytest.approx(0.08 * 147 / 365.0 * 100.0, abs=1e-9)


def test_bono_alineado_no_cambia():
    """Sin stub (emisión y vto en la misma grilla) el schedule queda idéntico:
    todos los cupones completos, incluido el del vencimiento."""
    cfs = build_on_cashflows(emission=date(2025, 11, 14), maturity=date(2030, 11, 14),
                             coupon_rate=0.078, coupon_freq=2)
    assert all(cf.interest == pytest.approx(3.9) for cf in cfs)


def test_fin_de_mes_no_se_confunde_con_un_stub():
    """IRSA 2035 (emisión 31-Mar-25, vto 31-Mar-35, semestral): el cupón del
    30-Sep→31-Mar mide 182 días por el clamp de fin de mes de relativedelta, NO es
    un período corto → tiene que seguir pagando el cupón completo."""
    cfs = build_on_cashflows(emission=date(2025, 3, 31), maturity=date(2035, 3, 31),
                             coupon_rate=0.06, coupon_freq=2)
    assert all(cf.interest == pytest.approx(3.0) for cf in cfs)
