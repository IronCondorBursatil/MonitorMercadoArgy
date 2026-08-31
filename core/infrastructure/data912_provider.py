"""Data912MarketDataProvider: market data live de data912.com (4 endpoints) +
histórico OHLC (bonos/acciones) con cache class-level invalidada por ciclo.

Extraído de repositories.py (M2.3): la lectura de precios live es un concern
distinto del parsing del catálogo Excel/Instrument. No comparte nada con el loader
(ningún helper de parsing) — solo http_get_json + el store de price_history.
"""

import logging
import os
import threading
import time
from datetime import date, timedelta
from typing import Dict, List, Optional

import pandas as pd

from core.domain.models import MarketSnapshot
from core.domain.interfaces import IMarketDataProvider
import httpx
from core.infrastructure.price_history import get_price_history_store

logger = logging.getLogger(__name__)


class Data912MarketDataProvider(IMarketDataProvider):
    ENDPOINTS = {
        "notes":  "https://data912.com/live/arg_notes",
        "bonds":  "https://data912.com/live/arg_bonds",
        "corp":   "https://data912.com/live/arg_corp",
        "stocks": "https://data912.com/live/arg_stocks",
    }
    UA = "balanz-monitor/1.0"

    # Per-ticker daily OHLC. Series only changes at end-of-day so the cache
    # can be hours-long without affecting freshness. Upstream rate-limit is
    # 120 req/min; with 20 Panel Líder tickers refreshed once per cache
    # window we stay well under.
    _STOCK_HISTORY_URL = "https://data912.com/historical/stocks/{ticker}"
    # Data912 SÍ expone histórico de bonos (OHLC diario, fresco a T-1): cubre
    # soberanos (todas las patas) + CER viejos (DICP/TX26). Devuelve {} (no 404)
    # para lo que no tiene: LECAP/tasa/TAMAR/DL/bopreales/ON/CER nuevos — esos los
    # cubre la acumulación del feed vivo (ver price_history.py).
    _BOND_HISTORY_URL = "https://data912.com/historical/bonds/{ticker}"
    _STOCK_HISTORY_TTL_S = 6 * 3600

    # Cache lives until explicit invalidation. The refresh loop calls
    # `invalidate_cache()` at the start of each cycle so every panel inside
    # the cycle reuses one network round-trip. A TTL alone wouldn't work:
    # when the cycle itself takes longer than the TTL, the cache expires
    # mid-cycle and every panel after that point re-hits the network (which
    # was the original bug). A long TTL as backstop catches CLI scripts and
    # other one-off callers that never invalidate.
    _CACHE_TTL_SEC = 60.0

    # CSV legacy (TSV, una columna por ticker): piso ESTÁTICO del read-path, debajo
    # del store vivo (price_history.py). Útil como seed/offline para los 17 tickers
    # que trae (soberanos D + bopreales). Ya NO es la fuente principal.
    _HISTORY_CSV = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "history", "precio_historico.csv",
    )

    # Class-level so multiple provider instances share the historical cache
    # (the heavy startup cost is paid once per process).
    _stock_history_cache: Dict[str, "tuple[float, list]"] = {}
    _bond_history_cache: Dict[str, "tuple[float, list]"] = {}
    _stock_history_lock = threading.Lock()

    # Timeout corto (4s): el ciclo de refresh es de ~5s; esperar más a un
    # endpoint colgado es desperdicio (el próximo ciclo reintenta igual).
    # 1 retry: con el keep-alive del pool (ver _http.py) el server puede
    # cerrar una conexión idle; el 1er request sobre ese socket muerto tira
    # RemoteDisconnected. El retry sale sobre una conexión fresca (~300ms) y
    # evita que un drop del pool tumbe los 4 endpoints por un ciclo entero.
    # Con timeout=4 + warm el peor caso del retry es chico (no como el viejo
    # spike de 40s que venía de timeout=10).
    _FETCH_TIMEOUT_S = 4
    _FETCH_RETRIES = 1

    # Dedupe de log durante outages. Sin esto, un upstream caído escupe ~140
    # líneas/min de ERROR+WARNING. Estrategia: log de la 1ª falla, log de
    # recovery, y un summary periódico mientras dura. Estado class-level
    # compartido entre instancias (main loop + BEI thread crean instancias
    # separadas pero ven el mismo Data912).
    _endpoint_state_lock = threading.Lock()
    _endpoint_state: Dict[str, dict] = {}  # {name: {ok, fail_count, first_fail_ts}}
    _last_degraded_summary_ts: float = 0.0
    _DEGRADED_SUMMARY_INTERVAL_S = 60.0

    def __init__(self):
        self._cache: Dict[str, dict] = {}
        self._cache_ts: float = 0.0
        self._history: Optional[Dict[str, Dict[date, float]]] = None
        self._history_lock = threading.Lock()

    def invalidate_cache(self) -> None:
        """Mark the cache as stale so the next fetch_snapshots refreshes from
        the network. Called by the refresh loop once per cycle."""
        self._cache_ts = 0.0

    @classmethod
    def _on_endpoint_success(cls, name: str) -> None:
        now = time.time()
        with cls._endpoint_state_lock:
            prev = cls._endpoint_state.get(name)
            if prev and not prev.get("ok", True):
                dur = now - prev["first_fail_ts"]
                logger.info(
                    "Data912/%s: recovered after %.0fs (%d failed cycles)",
                    name, dur, prev["fail_count"],
                )
            cls._endpoint_state[name] = {"ok": True, "fail_count": 0, "first_fail_ts": None}

    @classmethod
    def _on_endpoint_failure(cls, name: str, err: Exception) -> None:
        now = time.time()
        with cls._endpoint_state_lock:
            prev = cls._endpoint_state.get(name, {"ok": True, "fail_count": 0, "first_fail_ts": None})
            if prev.get("ok", True):
                # 1ª falla del run → log normal.
                logger.error("Data912/%s: %s: %s", name, type(err).__name__, err)
                cls._endpoint_state[name] = {"ok": False, "fail_count": 1, "first_fail_ts": now}
            else:
                # Ya está caído — sumamos al contador, no inundamos.
                prev["fail_count"] += 1

    @classmethod
    def _maybe_log_degraded_summary(cls) -> None:
        now = time.time()
        with cls._endpoint_state_lock:
            down = [(n, s) for n, s in cls._endpoint_state.items() if not s.get("ok", True)]
            if not down:
                return
            if (now - cls._last_degraded_summary_ts) < cls._DEGRADED_SUMMARY_INTERVAL_S:
                return
            cls._last_degraded_summary_ts = now
            max_dur = max(now - s["first_fail_ts"] for _, s in down)
            names = ",".join(sorted(n for n, _ in down))
            total = len(cls.ENDPOINTS)
        logger.warning(
            "Data912 degraded: %d/%d endpoints down for %.0fs: %s",
            len(down), total, max_dur, names,
        )

    def _fetch_all_endpoints(self):
        if self._cache and (time.monotonic() - self._cache_ts) < self._CACHE_TTL_SEC:
            return

        def _fetch_one(item):
            name, url = item
            try:
                resp = httpx.get(
                    url, timeout=self._FETCH_TIMEOUT_S, headers={"User-Agent": self.UA}
                )
                resp.raise_for_status()
                payload = resp.json()
                self._on_endpoint_success(name)
                return [(str(row.get("symbol", "")).upper(), row)
                        for row in payload if row.get("symbol")]
            except Exception as e:
                self._on_endpoint_failure(name, e)
                return []

        # 4 endpoints en paralelo. Con timeout=4 + sin retry, el peor caso de
        # un endpoint colgado es ~4s; en paralelo todos cuelgan a la vez.
        all_data: Dict[str, dict] = {}
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(self.ENDPOINTS)) as ex:
            for rows in ex.map(_fetch_one, list(self.ENDPOINTS.items())):
                for sym, row in rows:
                    all_data[sym] = row

        # Si TODOS los endpoints fallaron, mantenemos el cache stale en vez
        # de wipear a {} — sino el UI cliente queda en blanco durante todo
        # el outage. Mejor mostrar data vieja (~5s atrás) que nada.
        # Si algún endpoint OK, mergeamos (no clear): los símbolos del
        # endpoint caído mantienen su última lectura buena.
        if all_data:
            self._cache.update(all_data)
        self._cache_ts = time.monotonic()
        self._maybe_log_degraded_summary()

    def fetch_snapshots(self, tickers: List[str]) -> Dict[str, MarketSnapshot]:
        self._fetch_all_endpoints()
        snapshots = {}
        for ticker in tickers:
            t = str(ticker).upper()
            # _CER suffix is a display alias for the CER side of dual bonds (e.g. TXMJ8_CER → TXMJ8)
            market_t = t[:-4] if t.endswith("_CER") else t
            row = self._cache.get(market_t) or self._cache.get(t)
            if not row: continue
            try:
                snapshots[ticker] = MarketSnapshot(
                    instrument=None,
                    price=float(row.get("c", 0.0)),
                    last_update=date.today(),
                    bid=float(row["px_bid"]) if row.get("px_bid") else None,
                    ask=float(row["px_ask"]) if row.get("px_ask") else None,
                    volume=float(row["v"]) if row.get("v") else None,
                    operations=int(row["q_op"]) if row.get("q_op") else None,
                    change_pct=float(row["pct_change"]) if row.get("pct_change") else None,
                )
            except (TypeError, ValueError) as e:
                logger.warning(f"Error parsing row for {ticker}: {e}")
        return snapshots

    def _load_history(self) -> Dict[str, Dict[date, float]]:
        # Double-checked locking: cheap read first, then lock only on miss.
        if self._history is not None:
            return self._history
        with self._history_lock:
            if self._history is not None:
                return self._history
            self._history = self._read_history_csv()
            return self._history

    def _read_history_csv(self) -> Dict[str, Dict[date, float]]:
        history: Dict[str, Dict[date, float]] = {}
        if not os.path.isfile(self._HISTORY_CSV):
            logger.info(f"No historical CSV at {self._HISTORY_CSV}; variances unavailable.")
            self._history = history
            return history
        try:
            df = pd.read_csv(self._HISTORY_CSV, sep="\t")
            ts_col = df.columns[0]
            df[ts_col] = pd.to_datetime(df[ts_col], format="%m/%d/%Y", errors="coerce")
            for col in df.columns[1:]:
                # CSV column headers son tickers directos (AL30D, GD30D, etc.).
                key = str(col).upper().strip()
                series: Dict[date, float] = {}
                for ts, val in zip(df[ts_col], df[col]):
                    if pd.isna(ts) or pd.isna(val):
                        continue
                    try:
                        series[ts.date()] = float(val)
                    except (TypeError, ValueError):
                        continue
                if series:
                    history[key] = series
            logger.info(f"Loaded historical prices for {len(history)} instruments.")
        except Exception as e:
            logger.warning(f"Could not load historical CSV: {e}")
        return history

    def fetch_stock_history(self, ticker: str) -> List[dict]:
        """Daily OHLC for a stock. Sorted ascending by date. Cached for
        `_STOCK_HISTORY_TTL_S` because the series only changes once per
        trading day. Returns the stale cache on transient fetch failures
        rather than dropping the panel — historical data has no urgency."""
        t = str(ticker).upper().strip()
        cached = self._stock_history_cache.get(t)
        if cached and (time.monotonic() - cached[0]) < self._STOCK_HISTORY_TTL_S:
            return cached[1]
        url = self._STOCK_HISTORY_URL.format(ticker=t)
        try:
            resp = httpx.get(url, timeout=10.0, headers={"User-Agent": self.UA})
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError(f"expected list, got {type(data).__name__}")
            with self._stock_history_lock:
                self._stock_history_cache[t] = (time.monotonic(), data)
            return data
        except Exception as e:
            logger.warning(f"Stock history fetch {t} failed: {e}")
            return cached[1] if cached else []

    def fetch_bond_history(self, ticker: str) -> List[dict]:
        """Daily OHLC de un bono vía Data912 `/historical/bonds/{ticker}` (mismo
        contrato que `fetch_stock_history`). Lista `{date,o,h,l,c,v,...}` asc, o []
        si el ticker no está cubierto (el endpoint devuelve {} para letras/bopreales/
        ON). Usado por el priming del store (price_history.prime_from_data912)."""
        t = str(ticker).upper().strip()
        cached = self._bond_history_cache.get(t)
        if cached and (time.monotonic() - cached[0]) < self._STOCK_HISTORY_TTL_S:
            return cached[1]
        url = self._BOND_HISTORY_URL.format(ticker=t)
        try:
            resp = httpx.get(url, timeout=10.0, headers={"User-Agent": self.UA})
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):  # {} (no cubierto) → tratar como vacío
                data = []
            with self._stock_history_lock:
                self._bond_history_cache[t] = (time.monotonic(), data)
            return data
        except Exception as e:
            logger.warning(f"Bond history fetch {t} failed: {e}")
            return cached[1] if cached else []

    @classmethod
    def clear_history_cache(cls) -> None:
        """Libera el JSON crudo del priming histórico (~37 MB de RSS).

        El priming (`prime_from_data912`) baja la serie completa de cada ticker UNA
        vez y la persiste en el store SQLite; después de eso el read-path
        (`fetch_historical_prices`) sale 100% del store y nadie vuelve a leer estos
        dicts. El TTL de 6 h los mantenía vivos en memoria para nadie: es un cache
        de un solo uso. Se llama desde `_price_history_loop` tras primar."""
        with cls._stock_history_lock:
            cls._bond_history_cache.clear()
            cls._stock_history_cache.clear()

    def fetch_historical_prices(self, ticker: str, days: int) -> Dict[date, float]:
        """`{date: close}` mergeando el CSV legacy (piso estático) con el store vivo
        (price_history.py, que gana en fechas solapadas por ser más fresco/profundo).
        El read-path es 100% local: el store/priming los mantiene una task de fondo."""
        t = str(ticker).upper().strip()
        # Mismo alias que fetch_snapshots: el sufijo `_CER` (pata CER de un dual,
        # ej. TXMJ8_CER) cotiza/se acumula bajo el símbolo de mercado (TXMJ8).
        if t.endswith("_CER"):
            t = t[:-4]
        csv_series = self._load_history().get(t, {})
        store_series = get_price_history_store().get_series(t)
        if not csv_series and not store_series:
            return {}
        merged = {**csv_series, **store_series}
        if days <= 0:
            return merged
        cutoff = date.today() - timedelta(days=days)
        return {d: p for d, p in merged.items() if d >= cutoff}
