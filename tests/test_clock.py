"""Tests del helper de fecha de referencia fija para tests (M0.1)."""

from __future__ import annotations

from datetime import date

from tests._clock import ref_date, DEFAULT_REF_DATE


def test_ref_date_defaults_to_fixed_capture_date(monkeypatch):
    """Sin env, devuelve la fecha de captura fija (no today()) — anti-decaimiento."""
    monkeypatch.delenv("MONITOR_TEST_REF_DATE", raising=False)
    assert ref_date() == DEFAULT_REF_DATE
    assert isinstance(DEFAULT_REF_DATE, date)


def test_ref_date_parses_iso_override(monkeypatch):
    monkeypatch.setenv("MONITOR_TEST_REF_DATE", "2025-01-15")
    assert ref_date() == date(2025, 1, 15)


def test_ref_date_today_keyword_uses_real_today(monkeypatch):
    """`today` opt-in para correr ocasionalmente con la fecha real y detectar
    regresiones date-dependent."""
    monkeypatch.setenv("MONITOR_TEST_REF_DATE", "today")
    assert ref_date() == date.today()
