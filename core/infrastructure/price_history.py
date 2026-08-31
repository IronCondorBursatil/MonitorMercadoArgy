"""Store local persistente de precios de cierre diarios por ticker — la fuente
unificada para las variaciones (Sem/1M/3M/YTD/1A) de los paneles.

Por qué existe: el cómputo de rendimientos necesita un histórico de cierres, y
antes vivía en un CSV manual (`data/history/precio_historico.csv`, 17 tickers,
refrescado a mano → se desfasaba y daba ventanas engañosas, p.ej. "Sem" == "1M").
Este store lo complementa/reemplaza con fuentes que se mergean en el read-path,
todas escritas a un SQLite local en `%LOCALAPPDATA%\\monitor` (fuera del working tree de git,
que corrompe SQLite mid-write):

  1. **Priming de Data912** `/historical/bonds/{ticker}` — historia profunda y
     fresca (a T-1) para lo que el endpoint cubre: soberanos (todas las patas) y
     CER viejos (DICP/TX26). Corre 1×/día en background (`prime_from_data912`).
  2. **Priming de BYMA open** `seriesHistoricas/especies` (`prime_from_byma_historico`)
     — completa lo que Data912 NO cubre (bopreales, letras, ON, patas MEP/CABLE, CER
     nuevos): ~1 año por especie exacta, sin credenciales. Corre 1× tras el de Data912.
  3. **Acumulación diaria** del cierre del feed en vivo (`record_live_closes`) —
     mantiene TODO el universo al día rueda a rueda una vez primado.
  4. **CSV legacy** — sigue siendo un piso estático en el read-path del provider
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
from typing import Dict, List, Optional

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
            "  volume REAL,"            # monto operado del día (del feed vivo); NULL = sin dato
            "  PRIMARY KEY (ticker, day))"
        )
        # Migración aditiva para DBs viejas (creadas sin la col `volume`). Idempotente.
        cols = {r[1] for r in con.execute("PRAGMA table_info(price_history)")}
        if "volume" not in cols:
            con.execute("ALTER TABLE price_history ADD COLUMN volume REAL")
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

    def _write(self, rows) -> int:
        # Filas (ticker, day, close[, volume]). `volume` opcional → None = no toca la
        # col volume (COALESCE preserva la existente; un write close-only NO la borra).
        clean = []
        for row in rows:
            t, d, c = row[0], row[1], row[2]
            if c is None or c <= 0:
                continue
            v = row[3] if len(row) > 3 else None
            v = float(v) if (v is not None and v >= 0) else None
            clean.append((t.upper().strip(), d, float(c), v))
        if not clean:
            return 0
        with self._lock:
            try:
                with closing(self._connect()) as con, con:
                    con.executemany(
                        "INSERT INTO price_history (ticker, day, close, volume) VALUES (?,?,?,?) "
                        "ON CONFLICT(ticker, day) DO UPDATE SET close=excluded.close, "
                        "volume=COALESCE(excluded.volume, price_history.volume)",
                        [(t, d.isoformat(), c, v) for t, d, c, v in clean],
                    )
            except sqlite3.Error as e:
                logger.warning("price_history: write failed (%s)", e)
                return 0
            # Update incremental del cache (ya persistido en disco arriba). Si la
            # carga del cache falló (None), no lo tocamos: el próximo read reintenta
            # la carga completa desde disco (que ya incluye este write).
            self._ensure_cache()
            if self._cache is not None:
                for t, d, c, _v in clean:
                    self._cache.setdefault(t, {})[d] = c
        return len(clean)

    def upsert(self, ticker: str, points: Dict[date, float]) -> int:
        """Backfill de una serie completa de un ticker (idempotente por (ticker, day))."""
        return self._write([(ticker, d, c) for d, c in points.items()])

    def record_closes(self, closes: Dict[str, float], on_date: date,
                      volumes: Optional[Dict[str, float]] = None) -> int:
        """Acumula el cierre (y opcionalmente el monto operado) de muchos tickers en una
        fecha (un solo write batch). `volumes` ausente → la col volume no se toca."""
        vols = volumes or {}
        return self._write([(t, on_date, c, vols.get(t)) for t, c in closes.items()])


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


def _norm_hist_ticker(t) -> str:
    """Normaliza un ticker para el store de histórico: upper/strip + drop del alias
    `_CER` de un dual (TXMJ8_CER → TXMJ8), igual que el read-path
    (`fetch_historical_prices`). Sin esto, el priming guardaría bajo una clave muerta."""
    tk = (t or "").upper().strip()
    return tk[:-4] if tk.endswith("_CER") else tk


def byma_prime_candidates(wanted, store: PriceHistoryStore, attempted,
                          min_days: int) -> List[str]:
    """Tickers a primar de BYMA: los de `wanted` con < `min_days` cierres en el store
    que TODAVÍA no se intentaron (`attempted`). Normaliza (upper, sin `_CER`) y dedup.

    El set `attempted` (intentar 1× por proceso) corta dos patologías: re-primar en
    cada tick los tickers que BYMA no tiene (bonos nuevos sin historia en ningún lado)
    y dejar colgados los que fallaron parcialmente en el mismo lote que un éxito —
    ambos se intentan exactamente una vez. Un fallo transitorio se recupera al
    reiniciar (best-effort: no vale la pena el estado para reintentar acotado)."""
    out: List[str] = []
    seen = {(a or "").upper().strip() for a in attempted}
    for t in wanted:
        tk = _norm_hist_ticker(t)
        if not tk or tk in seen:
            continue
        seen.add(tk)
        if len(store.get_series(tk)) < min_days:
            out.append(tk)
    return out


def prime_from_byma_historico(tickers, store: PriceHistoryStore, *,
                              max_days: int = 400, max_workers: int = 4,
                              fetch=None) -> int:
    """Backfill de cierres diarios vía **series históricas de BYMA open** para los
    `tickers` dados — los que el `/historical/bonds` de Data912 NO cubre (bopreales,
    letras, ON, patas MEP/CABLE, CER nuevos). UPSERT al store. Devuelve cierres
    escritos. `fetch` (symbol, max_days) → {date: close} inyectable para tests.

    Best-effort por ticker: un fallo de red no aborta el resto. Pensado para correr
    1× en background (la historia BYMA llega ~1 año; el feed vivo la mantiene al día).

    Fuente según `settings.byma_history_source`: 'chart' (default — endpoint chart
    OHLCV, 1 llamada/ticker, cubre patas D/C y letras) o 'series' (POST
    seriesHistoricas, paginado 25d). `fetch` explícito gana sobre ambos (tests)."""
    if fetch is None:
        if settings.byma_history_source == "series":
            from core.infrastructure.byma.series_historicas import fetch_history as fetch
        else:
            from core.infrastructure.byma.chart_history import fetch_history as fetch
    uniq = list(dict.fromkeys(_norm_hist_ticker(t) for t in tickers if t))
    uniq = [t for t in uniq if t]
    if not uniq:
        return 0

    def _one(tk: str) -> int:
        try:
            points = fetch(tk, max_days=max_days)
        except Exception:  # noqa: BLE001 — best-effort por ticker
            return 0
        return store.upsert(tk, points) if points else 0

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
    volumes: Dict[str, float] = {}
    for sym, row in (snapshot_rows or {}).items():
        c = getattr(row, "c", None)
        if c and c > 0:
            closes[sym] = float(c)
            # `row.v` = monto operado del día (del feed). El UPSERT por (ticker,día) deja
            # la última escritura ≈ el total operado al cierre → base del promedio.
            v = getattr(row, "v", None)
            if v is not None and v > 0:
                volumes[sym] = float(v)
    return store.record_closes(closes, on_date, volumes=volumes)
