"""Store local persistente de precios de cierre diarios por ticker — la fuente
unificada para las variaciones (Sem/1M/3M/YTD/1A) de los paneles.

Por qué existe: el cómputo de rendimientos necesita un histórico de cierres, y
antes vivía en un CSV manual (`data/history/precio_historico.csv`, 17 tickers,
refrescado a mano → se desfasaba y daba ventanas engañosas, p.ej. "Sem" == "1M").
Este store lo complementa/reemplaza con fuentes que se mergean en el read-path,
todas escritas a un SQLite local en `%LOCALAPPDATA%\\monitor` (fuera de OneDrive,
que corrompe SQLite mid-write):

  1. **Priming de Data912** `/historical/bonds/{ticker}` — historia profunda y
     fresca (a T-1) para lo que el endpoint cubre: soberanos (todas las patas) y
     CER viejos (DICP/TX26). Corre 1×/día en background (`prime_from_data912`).
  2. **Acumulación diaria** del cierre del feed en vivo (`record_live_closes`) —
     cubre TODO el universo del monitor con el tiempo (bopreales, LECAP, TAMAR,
     DL, ON, CER nuevos), que el endpoint histórico NO tiene.
  3. **CSV legacy** — sigue siendo un piso estático en el read-path del provider
     (`Data912MarketDataProvider.fetch_historical_prices`), bajo el store.

El **read-path** (`get_series`) es 100% local (cache en memoria, sin red) → no
toca el hot-path de pricing ni los tests. El **write-path** (priming + acumulación)
corre en una task de background (prod); bajo pytest los loops no arrancan, así que
el store queda vacío y el provider cae al CSV (comportamiento previo, determinístico).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
from typing import Dict, List, Optional, Tuple

from config.settings import settings

logger = logging.getLogger(__name__)


class PriceHistoryStore:
    """Cierres diarios `{ticker: {date: close}}` sobre SQLite (`price_history`).

    Thread-safe: una conexión efímera por operación (sqlite3 es seguro así) + un
    `RLock` que protege el cache en memoria. El cache se carga lazy una vez y se
    actualiza incrementalmente en cada write, así el read-path no re-consulta disco.
    """

    def __init__(self, db_path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, Dict[date, float]]] = None

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS price_history ("
            "  ticker TEXT NOT NULL,"
            "  day    TEXT NOT NULL,"   # ISO 'YYYY-MM-DD'
            "  close  REAL NOT NULL,"
            "  PRIMARY KEY (ticker, day))"
        )
        return con

    def _ensure_cache(self) -> None:
        if self._cache is not None:
            return
        cache: Dict[str, Dict[date, float]] = {}
        try:
            with closing(self._connect()) as con:
                for tk, day, close in con.execute(
                        "SELECT ticker, day, close FROM price_history"):
                    try:
                        cache.setdefault(tk, {})[date.fromisoformat(day)] = float(close)
                    except (TypeError, ValueError):
                        continue
        except sqlite3.Error as e:
            # NO latchear: dejamos `_cache=None` para reintentar la carga en la
            # próxima llamada. Si cacheáramos el dict vacío, un error transitorio de
            # SQLite (lock/IO en Windows) dejaría las variaciones en blanco para todo
            # el proceso, incluso con el histórico ya persistido en disco.
            logger.warning("price_history: load failed, will retry (%s)", e)
            return
        self._cache = cache

    def get_series(self, ticker: str) -> Dict[date, float]:
        """`{date: close}` para un ticker (vacío si no hay). Copia bajo lock: el
        caller (read-path de pricing, en un thread del pool) puede iterarla mientras
        la task de fondo escribe el cache, sin 'dict changed size during iteration'."""
        t = (ticker or "").upper().strip()
        with self._lock:
            self._ensure_cache()
            if self._cache is None:   # carga falló → sin dato este ciclo (reintenta)
                return {}
            return dict(self._cache.get(t, {}))

    def _write(self, rows: List[Tuple[str, date, float]]) -> int:
        clean = [(t.upper().strip(), d, float(c))
                 for t, d, c in rows if c is not None and c > 0]
        if not clean:
            return 0
        with self._lock:
            try:
                with closing(self._connect()) as con, con:
                    con.executemany(
                        "INSERT INTO price_history (ticker, day, close) VALUES (?,?,?) "
                        "ON CONFLICT(ticker, day) DO UPDATE SET close=excluded.close",
                        [(t, d.isoformat(), c) for t, d, c in clean],
                    )
            except sqlite3.Error as e:
                logger.warning("price_history: write failed (%s)", e)
                return 0
            # Update incremental del cache (ya persistido en disco arriba). Si la
            # carga del cache falló (None), no lo tocamos: el próximo read reintenta
            # la carga completa desde disco (que ya incluye este write).
            self._ensure_cache()
            if self._cache is not None:
                for t, d, c in clean:
                    self._cache.setdefault(t, {})[d] = c
        return len(clean)

    def upsert(self, ticker: str, points: Dict[date, float]) -> int:
        """Backfill de una serie completa de un ticker (idempotente por (ticker, day))."""
        return self._write([(ticker, d, c) for d, c in points.items()])

    def record_closes(self, closes: Dict[str, float], on_date: date) -> int:
        """Acumula el cierre de muchos tickers en una fecha (un solo write batch)."""
        return self._write([(t, on_date, c) for t, c in closes.items()])


# --------------------------------------------------------------------------- #
# Singleton de proceso (read-path del provider + write-path de la task comparten
# el mismo store/cache). El path se resuelve de `settings` (override por env en
# tests vía MONITOR_PRICE_HISTORY_DB → conftest lo aísla a un temp).
# --------------------------------------------------------------------------- #
_STORE: Optional[PriceHistoryStore] = None
_STORE_LOCK = threading.Lock()


def get_price_history_store() -> PriceHistoryStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = PriceHistoryStore(settings.price_history_db)
    return _STORE


# --------------------------------------------------------------------------- #
# Write-path (background, prod). Best-effort: nunca propaga excepciones al loop.
# --------------------------------------------------------------------------- #
def prime_from_data912(tickers, provider, store: PriceHistoryStore,
                       max_workers: int = 8) -> int:
    """Backfill profundo: `/historical/bonds/{t}` por cada ticker (los no cubiertos
    devuelven [] → se saltan) → UPSERT al store. Devuelve cierres escritos."""
    uniq = list(dict.fromkeys((t or "").upper().strip() for t in tickers if t))

    def _one(tk: str) -> int:
        try:
            bars = provider.fetch_bond_history(tk)
        except Exception:  # noqa: BLE001 — best-effort por ticker
            return 0
        points: Dict[date, float] = {}
        for b in bars or ():
            try:
                points[date.fromisoformat(b["date"])] = float(b["c"])
            except (KeyError, TypeError, ValueError):
                continue
        return store.upsert(tk, points) if points else 0

    if not uniq:
        return 0
    total = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for n in ex.map(_one, uniq):
            total += n
    return total


def record_live_closes(snapshot_rows: dict, store: PriceHistoryStore,
                       on_date: date) -> int:
    """Acumula el "cierre" de hoy de todo el snapshot vivo del hub (`{symbol: row}`
    con `row.c`). Cubre el universo que el endpoint histórico no tiene.

    `row.c` es el ÚLTIMO precio al momento de la escritura (no el cierre oficial); el
    UPSERT por (ticker, día) hace que la última escritura del día ≈ el cierre. Para
    los tickers que cubre el priming de Data912, el cierre real lo pisa en el próximo
    arranque; para los no cubiertos, si el server para antes del cierre ese día queda
    en un valor intradía (impacto chico: hoy nunca es base de una ventana)."""
    closes: Dict[str, float] = {}
    for sym, row in (snapshot_rows or {}).items():
        c = getattr(row, "c", None)
        if c and c > 0:
            closes[sym] = float(c)
    return store.record_closes(closes, on_date)
