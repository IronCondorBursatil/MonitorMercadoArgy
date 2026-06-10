"""Tests de core/domain/inflation_path.py (M3a) — sin cobertura previa.

Sendero de inflación implícita mensual (NT 8/2024 Fig. 4): forwards Fisher entre
bordes de mes contra curvas nominal/real. Se usan curvas planas deterministas."""

from __future__ import annotations

from datetime import date

import pytest

from core.domain.inflation_path import monthly_inflation_path, MonthlyBEI


def _flat(rate):
    return lambda t: rate


def test_returns_requested_number_of_months():
    path = monthly_inflation_path(_flat(0.30), _flat(0.10), date(2026, 1, 1), months_ahead=6)
    assert len(path) == 6
    assert all(isinstance(m, MonthlyBEI) for m in path)


def test_annualized_bei_matches_fisher_for_flat_curves():
    # Curvas planas: el forward de cada mes = nivel → BEI anual = Fisher(0.30,0.10).
    path = monthly_inflation_path(_flat(0.30), _flat(0.10), date(2026, 1, 1), months_ahead=4)
    fisher = (1.30 / 1.10) - 1.0
    for m in path[1:]:   # el 1er mes es parcial (spot), igual debería rondar
        assert m.annualized_bei == pytest.approx(fisher, abs=1e-6)


def test_monthly_bei_is_positive_and_below_annual():
    path = monthly_inflation_path(_flat(0.30), _flat(0.10), date(2026, 1, 10), months_ahead=6)
    for m in path:
        if m.monthly_bei is not None:
            assert 0 < m.monthly_bei < m.annualized_bei  # mensual < anualizado


def test_labels_are_spanish_month_year():
    path = monthly_inflation_path(_flat(0.30), _flat(0.10), date(2026, 5, 1), months_ahead=3)
    assert path[0].label == "may-26"
    assert path[1].label == "jun-26"
    assert path[2].label == "jul-26"


def test_year_rollover_in_labels():
    path = monthly_inflation_path(_flat(0.30), _flat(0.10), date(2026, 11, 15), months_ahead=3)
    labels = [m.label for m in path]
    assert "ene-27" in labels   # cruza el fin de año


def test_short_first_segment_is_skipped():
    # Arrancando casi a fin de mes, el 1er segmento tiene < min_segment_days → se saltea.
    path = monthly_inflation_path(_flat(0.30), _flat(0.10), date(2026, 1, 29),
                                  months_ahead=3, min_segment_days=5)
    # enero arranca el 29 → 3 días de cobertura < 5 → primer label es febrero
    assert path[0].label == "feb-26"


def test_days_in_month_is_calendar_correct():
    path = monthly_inflation_path(_flat(0.30), _flat(0.10), date(2026, 2, 1), months_ahead=1)
    assert path[0].days_in_month == 28   # feb 2026 no bisiesto
