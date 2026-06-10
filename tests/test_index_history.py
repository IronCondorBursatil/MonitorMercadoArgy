"""Store de muestras de índices (core.infrastructure.byma.index_history):
record/series/prune + prime_from_chart con fetch inyectado (sin red)."""

import datetime as dt

from core.infrastructure.byma.index_history import (
    IndexHistoryStore, prime_from_chart, window_start,
)


def test_record_and_series_since_ordered(tmp_path):
    s = IndexHistoryStore(str(tmp_path / "i.db"))
    s.record_many([("M", "2026-06-08T11:05:00", 101.0),
                   ("M", "2026-06-08T11:00:00", 100.0),
                   ("G", "2026-06-08T11:00:00", 200.0)])
    ser = s.series_since("2026-06-08T00:00:00")
    assert [v for _, v in ser["M"]] == [100.0, 101.0]      # ordenado por ts
    assert ser["G"] == [("2026-06-08T11:00:00", 200.0)]


def test_series_since_filters_window(tmp_path):
    s = IndexHistoryStore(str(tmp_path / "i.db"))
    s.record_many([("M", "2026-06-01T17:00:00", 90.0),
                   ("M", "2026-06-08T11:00:00", 100.0)])
    ser = s.series_since("2026-06-05T00:00:00")
    assert [v for _, v in ser["M"]] == [100.0]             # la muestra vieja queda afuera


def test_record_upsert_dedups_same_ts(tmp_path):
    s = IndexHistoryStore(str(tmp_path / "i.db"))
    s.record("M", "2026-06-08T11:00:00", 100.0)
    s.record("M", "2026-06-08T11:00:00", 105.0)            # mismo ts → upsert
    assert s.series_since("2026-06-08T00:00:00")["M"] == [("2026-06-08T11:00:00", 105.0)]


def test_prune_removes_old(tmp_path):
    s = IndexHistoryStore(str(tmp_path / "i.db"))
    s.record_many([("M", "2026-06-01T17:00:00", 90.0),
                   ("M", "2026-06-08T11:00:00", 100.0)])
    s.prune("2026-06-05T00:00:00")
    assert [v for _, v in s.series_since("2026-01-01T00:00:00")["M"]] == [100.0]


def test_prime_from_chart_writes_daily_anchors(tmp_path):
    s = IndexHistoryStore(str(tmp_path / "i.db"))

    def fake(code, max_days=90):
        return ({dt.date(2026, 6, 4): 3000000.0, dt.date(2026, 6, 5): 3050000.0}
                if code == "M" else {})

    n = prime_from_chart(s, codes=("M", "G"), ruedas=5, fetch=fake)
    assert n == 2
    ser = s.series_since("2026-06-01T00:00:00")
    assert ser["M"] == [("2026-06-04T17:00:00", 3000000.0),
                        ("2026-06-05T17:00:00", 3050000.0)]
    assert "G" not in ser                                  # G devolvió {} → sin filas


def test_window_start():
    assert window_start(dt.date(2026, 6, 8), 5).startswith("2026-06-01")  # 8-(5+2)=1
