"""BCRAIndicesProvider: ventana de fetch bootstrap-vs-topup (lente largo del panel FCI)."""

from datetime import date, timedelta

from core.infrastructure.indices_provider import BCRAIndicesProvider

W = BCRAIndicesProvider._fetch_window


def test_empty_cache_bootstraps():
    assert W({}, 400, 30) == 400


def test_short_history_backfills():
    # cache con ~7 semanas (caso real CER/A3500) → todavía pide la ventana completa
    today = date.today()
    short = {today - timedelta(days=i): 1.0 for i in range(50)}
    assert W(short, 400, 30) == 400


def test_full_history_tops_up():
    today = date.today()
    full = {today - timedelta(days=i): 1.0 for i in range(0, 400, 7)}  # ~400d cubiertos
    assert W(full, 400, 30) == 30
