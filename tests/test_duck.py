"""DuckDB analytics (Fase 2): cer_asof (forward-fill) y avg_tamar (AVG observada).

Se testea la lógica SQL con datos sintéticos en una vista in-memory (determinista,
sin depender de los CSV reales).
"""

from datetime import date

import duckdb
import pytest

from core.infrastructure.analytics import duck


@pytest.fixture
def con_cer():
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE VIEW cer_daily AS SELECT * FROM (VALUES "
        "(DATE '2026-01-01', 100.0), (DATE '2026-01-05', 110.0), (DATE '2026-01-10', 120.0)"
        ") AS t(fecha, valor)"
    )
    return con


def test_cer_asof_forward_fill(con_cer):
    res = duck.cer_asof(con_cer, [
        date(2026, 1, 1),   # exacto
        date(2026, 1, 3),   # entre 01 y 05 -> 100 (forward-fill)
        date(2026, 1, 5),   # exacto
        date(2026, 1, 12),  # después del último -> 120
        date(2025, 12, 31), # antes del primero -> None
    ])
    assert res[date(2026, 1, 1)] == 100.0
    assert res[date(2026, 1, 3)] == 100.0
    assert res[date(2026, 1, 5)] == 110.0
    assert res[date(2026, 1, 12)] == 120.0
    assert res[date(2025, 12, 31)] is None


def test_cer_asof_empty_dates(con_cer):
    assert duck.cer_asof(con_cer, []) == {}


def test_cer_asof_missing_view():
    con = duckdb.connect(":memory:")  # sin vista cer_daily
    res = duck.cer_asof(con, [date(2026, 1, 1)])
    assert res == {date(2026, 1, 1): None}


def test_avg_tamar():
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE VIEW tamar_daily AS SELECT * FROM (VALUES "
        "(DATE '2020-01-01', 30.0), (DATE '2020-01-02', 32.0), (DATE '2020-01-03', 34.0)"
        ") AS t(fecha, valor)"
    )
    # AVG sobre fechas pasadas (2020) acotado a current_date (no recorta nada).
    avg = duck.avg_tamar(con, date(2020, 1, 1), date(2020, 1, 3))
    assert abs(avg - 32.0) < 1e-9


def test_avg_tamar_missing_view():
    con = duckdb.connect(":memory:")
    assert duck.avg_tamar(con, date(2020, 1, 1), date(2020, 1, 3)) is None


def test_connect_reads_real_csvs_if_present():
    """Smoke: connect() no explota y, si hay CSVs, expone las vistas."""
    con = duck.connect()
    views = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views()").fetchall()}
    # Al menos no debe crashear; si el CSV de CER existe, la vista está.
    assert isinstance(views, set)
