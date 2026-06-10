"""Store local de muestras de índices BYMA para la franja de 5 ruedas del catálogo.

BYMA open NO expone intradiario histórico de índices (los endpoints `intra/*` dan
401, el chart da `no_data` en resolución intradía) y solo M/G tienen serie diaria.
Para que LOS 16 índices muestren un sparkline denso necesitamos acumular nosotros:

  1. **Backfill** de M/G desde el chart endpoint (cierres diarios → anclas).
  2. **Acumulación** del valor de los 16 desde `/index-price` cada poll (una muestra
     timestamped por código) — densifica la curva durante la rueda.

Se persiste a SQLite (fuera de OneDrive) → sobrevive reinicios y va completando las
5 ruedas. El read-path devuelve las muestras de la ventana (últimas N ruedas) y se
podan las viejas. Mismo patrón que `price_history.py`.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import closing
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from config.settings import settings

logger = logging.getLogger(__name__)


class IndexHistoryStore:
    """Muestras `(code, ts, value)` sobre SQLite. `ts` ISO 'YYYY-MM-DDTHH:MM:SS'
    (un cierre diario se guarda con ts 'YYYY-MM-DDT17:00:00'). Thread-safe vía
    conexiones efímeras (sqlite3 es seguro así)."""

    def __init__(self, db_path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS index_history ("
            "  code  TEXT NOT NULL,"
            "  ts    TEXT NOT NULL,"   # ISO 'YYYY-MM-DDTHH:MM:SS'
            "  value REAL NOT NULL,"
            "  PRIMARY KEY (code, ts))"
        )
        return con

    def record_many(self, rows: List[Tuple[str, str, float]]) -> int:
        """Upsert de muestras `(code, ts, value)` (idempotente por (code, ts))."""
        clean = [(str(c).upper(), str(t), float(v)) for c, t, v in rows
                 if c and t and v is not None and v > 0]
        if not clean:
            return 0
        with self._lock:
            try:
                with closing(self._connect()) as con, con:
                    con.executemany(
                        "INSERT INTO index_history (code, ts, value) VALUES (?,?,?) "
                        "ON CONFLICT(code, ts) DO UPDATE SET value=excluded.value", clean)
            except sqlite3.Error as e:
                logger.warning("index_history: write failed (%s)", e)
                return 0
        return len(clean)

    def record(self, code: str, ts: str, value: float) -> int:
        return self.record_many([(code, ts, value)])

    def series_since(self, since_iso: str) -> Dict[str, List[Tuple[str, float]]]:
        """`{code: [(ts, value), ...]}` ordenado por ts, con ts >= `since_iso`."""
        out: Dict[str, List[Tuple[str, float]]] = {}
        with self._lock:
            try:
                with closing(self._connect()) as con:
                    for code, ts, value in con.execute(
                            "SELECT code, ts, value FROM index_history "
                            "WHERE ts >= ? ORDER BY code, ts", (since_iso,)):
                        out.setdefault(code, []).append((ts, float(value)))
            except sqlite3.Error as e:
                logger.warning("index_history: read failed (%s)", e)
                return {}
        return out

    def prune(self, before_iso: str) -> None:
        """Borra muestras con ts < `before_iso` (mantiene la tabla acotada)."""
        with self._lock:
            try:
                with closing(self._connect()) as con, con:
                    con.execute("DELETE FROM index_history WHERE ts < ?", (before_iso,))
            except sqlite3.Error as e:
                logger.warning("index_history: prune failed (%s)", e)


_STORE: Optional[IndexHistoryStore] = None
_STORE_LOCK = threading.Lock()


def get_index_history_store() -> IndexHistoryStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = IndexHistoryStore(settings.index_history_db)
    return _STORE


def prime_from_chart(store: IndexHistoryStore, codes=("M", "G"), *,
                     ruedas: int = 5, fetch=None) -> int:
    """Backfill de cierres diarios de M/G (los únicos con chart) como anclas de la
    ventana. `fetch(code, max_days)` inyectable para tests. Devuelve muestras escritas."""
    if fetch is None:
        from core.infrastructure.byma.chart_history import fetch_index_history as fetch
    total = 0
    for code in codes:
        series = fetch(code, max_days=ruedas + 4) or {}
        rows = [(code, f"{d.isoformat()}T17:00:00", c) for d, c in series.items()]
        total += store.record_many(rows)
    return total


def window_start(today: date, ruedas: int) -> str:
    """ISO de inicio de la ventana de `ruedas` ruedas (aprox, incluye findes)."""
    return (today - timedelta(days=ruedas + 2)).isoformat() + "T00:00:00"
