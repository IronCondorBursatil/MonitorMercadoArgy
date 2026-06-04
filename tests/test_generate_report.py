"""Helpers de GenerateMonitorReport: lookup as-of del histórico de precios."""

from datetime import date

from core.use_cases.generate_report import _asof_price


def _series():
    # Ruedas con huecos (sin sáb 10 / dom 11), como el CSV real.
    return {
        date(2026, 1, 2): 100.0,
        date(2026, 1, 9): 105.0,
        date(2026, 5, 8): 120.0,  # último dato disponible (CSV desfasado)
    }


def test_asof_exact_date():
    assert _asof_price(_series(), date(2026, 1, 9)) == 105.0


def test_asof_falls_back_to_previous_trading_day():
    # 11-ene (domingo) no tiene rueda → toma el 9-ene.
    assert _asof_price(_series(), date(2026, 1, 11)) == 105.0


def test_asof_target_after_last_row_uses_last_available():
    # Pedimos "hoy - 7d" pero el CSV terminó hace semanas → último cierre.
    assert _asof_price(_series(), date(2026, 6, 1)) == 120.0


def test_asof_before_first_row_is_none():
    assert _asof_price(_series(), date(2025, 12, 31)) is None


def test_asof_empty_series_is_none():
    assert _asof_price({}, date(2026, 1, 1)) is None


def test_asof_tolerance_rejects_stale_base():
    # Con tol_days, una base demasiado vieja respecto al objetivo se descarta
    # (evita que "Sem" caiga a un cierre de hace un mes y dé igual que "1M").
    s = {date(2026, 1, 9): 105.0}
    # objetivo 16-ene: base 9-ene está 7 días antes → fuera de tol=5.
    assert _asof_price(s, date(2026, 1, 16), tol_days=5) is None
    # mismo objetivo, tolerancia amplia → la acepta.
    assert _asof_price(s, date(2026, 1, 16), tol_days=10) == 105.0
    # base dentro de tol (finde): 11-ene (domingo) → 9-ene, 2 días, ok.
    assert _asof_price(s, date(2026, 1, 11), tol_days=5) == 105.0
