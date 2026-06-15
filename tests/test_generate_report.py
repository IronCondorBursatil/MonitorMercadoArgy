"""Helpers de GenerateMonitorReport: lookup as-of del histórico de precios."""

from datetime import date, timedelta

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


# ── C5: memoización de bases históricas por (ticker, día) ───────────────────

def test_hist_bases_cache_hit_avoids_second_fetch():
    """C5: segunda llamada el mismo día para el mismo ticker devuelve el resultado
    cacheado sin volver a llamar a fetch_historical_prices."""
    from unittest.mock import MagicMock
    from core.use_cases.generate_report import _hist_bases, _HIST_BASE_CACHE

    _HIST_BASE_CACHE.clear()

    provider = MagicMock()
    provider.fetch_historical_prices.return_value = {}

    today = date(2026, 6, 15)
    _hist_bases("AL30", today, provider)
    _hist_bases("AL30", today, provider)  # segundo call

    provider.fetch_historical_prices.assert_called_once()  # fetch solo 1 vez


def test_hist_bases_cache_miss_on_day_rollover():
    """C5: nuevo día invalida el cache y dispara un nuevo fetch."""
    from unittest.mock import MagicMock
    from core.use_cases.generate_report import _hist_bases, _HIST_BASE_CACHE

    _HIST_BASE_CACHE.clear()

    provider = MagicMock()
    provider.fetch_historical_prices.return_value = {}

    _hist_bases("AL30", date(2026, 6, 14), provider)
    _hist_bases("AL30", date(2026, 6, 15), provider)  # día siguiente

    assert provider.fetch_historical_prices.call_count == 2


def test_hist_bases_returns_correct_tuple():
    """C5: las 5 bases se calculan correctamente desde el hist."""
    from core.use_cases.generate_report import _hist_bases, _HIST_BASE_CACHE
    from unittest.mock import MagicMock

    _HIST_BASE_CACHE.clear()

    today = date(2026, 6, 15)
    hist = {
        today - timedelta(days=7):   100.0,  # Sem
        today - timedelta(days=30):  90.0,   # 1M
        today - timedelta(days=90):  80.0,   # 3M
        date(2025, 12, 31):          70.0,   # YTD base (31-dic del año anterior)
        today - timedelta(days=365): 60.0,   # 1A
    }
    provider = MagicMock()
    provider.fetch_historical_prices.return_value = hist

    px_7d, px_30d, px_90d, px_ytd, px_1y = _hist_bases("GD30", today, provider)
    assert px_7d == 100.0
    assert px_30d == 90.0
    assert px_90d == 80.0
    assert px_ytd == 70.0
    assert px_1y == 60.0


# ── C6: injectable providers ─────────────────────────────────────────────────

def test_injectable_indices_and_fx_are_used_not_reinstantiated():
    """C6: los providers inyectados en __init__ se usan en execute()
    en lugar de crear instancias nuevas en cada llamada."""
    from unittest.mock import MagicMock, patch
    from core.use_cases.generate_report import GenerateMonitorReport

    mock_indices = MagicMock()
    mock_fx = MagicMock()
    mock_fx.get_quote.return_value = {"venta": 1000.0}

    mock_repo = MagicMock()
    mock_repo.get_instruments_by_type.return_value = []
    mock_provider = MagicMock()
    mock_provider.fetch_snapshots.return_value = {}

    uc = GenerateMonitorReport(mock_repo, mock_provider,
                               indices=mock_indices, fx=mock_fx)

    # BCRAIndicesProvider y DolarAPIProvider NO deben instanciarse.
    with patch("core.use_cases.generate_report.BCRAIndicesProvider") as mock_bcra, \
         patch("core.use_cases.generate_report.DolarAPIProvider") as mock_dolar:
        uc.execute(["BONAR"])
        mock_bcra.assert_not_called()
        mock_dolar.assert_not_called()

    # Los mocks inyectados sí son los que se pasan a get_quote.
    mock_fx.get_quote.assert_called()


def test_default_providers_created_when_not_injected():
    """C6: sin inyección (retro-compat), BCRAIndicesProvider y DolarAPIProvider
    se crean dentro de execute()."""
    from unittest.mock import MagicMock, patch
    from core.use_cases.generate_report import GenerateMonitorReport

    mock_repo = MagicMock()
    mock_repo.get_instruments_by_type.return_value = []
    mock_provider = MagicMock()
    mock_provider.fetch_snapshots.return_value = {}

    uc = GenerateMonitorReport(mock_repo, mock_provider)  # sin inyección

    with patch("core.use_cases.generate_report.BCRAIndicesProvider") as mock_bcra, \
         patch("core.use_cases.generate_report.DolarAPIProvider") as mock_dolar:
        mock_dolar.return_value.get_quote.return_value = None
        uc.execute(["BONAR"])
        mock_bcra.assert_called_once()
        mock_dolar.assert_called_once()
