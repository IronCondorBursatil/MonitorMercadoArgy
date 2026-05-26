"""Analytics OLAP sobre las series históricas vía DuckDB (deuda #8).

Reemplaza los loops Python día-a-día por SQL:
  - `cer_asof`: el valor CER vigente a cada fecha (forward-fill) — reemplaza el
    scan de 14 días de `BCRAIndicesProvider._lookup`.
  - `avg_tamar`: promedio de TAMAR observada sobre un período — reemplaza el loop
    día-a-día de `tamar.avg_tamar_tna` (parte observada; la futura/forecast se
    combina en Python como hoy).

Las vistas se crean sólo si el CSV existe (defensivo para tests/CLI sin data).
"""

from __future__ import annotations

import os
from datetime import date
from typing import Dict, List, Optional

import duckdb

from config.settings import settings


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    h = settings.history_dir.as_posix()
    sources = {
        "cer_daily": f"{h}/cer_diario.csv",
        "tamar_daily": f"{h}/tamar_diario.csv",
        "a3500_daily": f"{h}/a3500_diario.csv",
    }
    for view, path in sources.items():
        if os.path.isfile(path):
            con.execute(
                f"CREATE VIEW {view} AS "
                f"SELECT CAST(fecha AS DATE) AS fecha, CAST(valor AS DOUBLE) AS valor "
                f"FROM read_csv_auto('{path}')"
            )
    return con


def _has_view(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return con.execute(
        "SELECT count(*) FROM duckdb_views() WHERE view_name = ?", [name]
    ).fetchone()[0] > 0


def cer_asof(con: duckdb.DuckDBPyConnection, ref_dates: List[date]) -> Dict[date, Optional[float]]:
    """{ref: CER vigente <= ref} (forward-fill). Reemplaza el loop de 14 días."""
    if not ref_dates or not _has_view(con, "cer_daily"):
        return {d: None for d in ref_dates}
    con.execute("CREATE OR REPLACE TEMP TABLE targets(ref DATE)")
    con.executemany("INSERT INTO targets VALUES (?)", [(d,) for d in ref_dates])
    rows = con.execute(
        """
        SELECT t.ref,
               (SELECT c.valor FROM cer_daily c
                WHERE c.fecha <= t.ref ORDER BY c.fecha DESC LIMIT 1) AS valor
        FROM targets t
        """
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def avg_tamar(con: duckdb.DuckDBPyConnection, start: date, end: date) -> Optional[float]:
    """AVG(TAMAR observada) sobre [start, min(end, hoy)]. None si no hay datos."""
    if not _has_view(con, "tamar_daily"):
        return None
    row = con.execute(
        "SELECT AVG(valor) FROM tamar_daily WHERE fecha BETWEEN ? AND least(?, current_date)",
        [start, end],
    ).fetchone()
    return row[0] if row else None
