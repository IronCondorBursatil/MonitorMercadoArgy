import logging
import os
import threading
import time
import warnings

import pandas as pd
from typing import List, Dict, Optional
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from core.domain.models import Instrument, Cashflow, MarketSnapshot
from core.domain.interfaces import IInstrumentsRepository, IMarketDataProvider
from core.infrastructure._http import http_get_json

# Silence unnecessary pandas warnings for cleaner console
warnings.filterwarnings("ignore", category=UserWarning, module="pandas")

logger = logging.getLogger(__name__)


def _infer_payment_frequency(cashflows) -> int:
    """Best-guess annual payment frequency from observed cashflow dates.
    Single-flow bonds → 1 (degenerate case; MD bullet formula applies).
    Multi-flow: median gap in months → 12/gap, rounded to common frequencies."""
    if not cashflows or len(cashflows) < 2:
        return 1
    dates = sorted(cf.date for cf in cashflows)
    gaps_days = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    if not gaps_days:
        return 1
    median_gap = sorted(gaps_days)[len(gaps_days) // 2]
    if median_gap <= 0:
        return 2
    freq = round(365.0 / median_gap)
    # Snap to standard market frequencies.
    for std in (12, 4, 2, 1):
        if abs(freq - std) <= 1:
            return std
    return max(1, min(12, freq))


class ExcelInstrumentsRepository(IInstrumentsRepository):
    NON_INSTRUMENT_SHEETS = frozenset({"Cashflows", "Cashflows_Fija", "Metadata", "Cotizaciones"})

    def __init__(self, excel_path: str):
        self.excel_path = excel_path
        self._cache_instruments: List[Instrument] = []
        self._by_ticker: Dict[str, Instrument] = {}
        self._by_type: Dict[str, List[Instrument]] = {}
        self._load_all()

    def _parse_date(self, val) -> Optional[date]:
        """Safely parse date from various formats without warnings.

        Critical: detect ISO strings (YYYY-MM-DD) and parse with dayfirst=False.
        pandas with dayfirst=True swaps month/day on ISO strings — e.g.
        "2026-07-09" becomes 2026-09-07. The Cashflows sheet stores AL29 dates
        as ISO strings, so this swap silently corrupted every soberano's
        coupon schedule before this fix.
        """
        if pd.isna(val) or val is None:
            return None
        if isinstance(val, pd.Timestamp):
            return val.date()
        if isinstance(val, (date, datetime)):
            return val.date() if hasattr(val, 'date') else val

        s = str(val).strip()
        if not s:
            return None
        # ISO format YYYY-MM-DD[...]: year first, MM and DD unambiguous.
        iso_like = (len(s) >= 10 and s[4] == "-" and s[7] == "-" and s[:4].isdigit())

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                dt = pd.to_datetime(s, dayfirst=not iso_like, errors='coerce')
                return dt.date() if pd.notna(dt) else None
            except (ValueError, TypeError):
                return None

    @staticmethod
    def _safe_int(val, default=0):
        try:
            if pd.isna(val):
                return default
            return int(float(val))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(val, default=0.0):
        try:
            if pd.isna(val):
                return default
            return float(val)
        except (TypeError, ValueError):
            return default

    def _get_date(self, row: pd.Series, candidates):
        for c in candidates:
            if c in row.index:
                d = self._parse_date(row[c])
                if d:
                    return d
        return None

    def _parse_coupon_rate(self, raw, asof: date):
        """Return coupon as decimal (0.05 = 5%). Supports step-up schedules
        like '2003-12-31:0.63;2009-03-31:1.18' (picks rate active at `asof`).
        """
        if raw is None or (not isinstance(raw, str) and pd.isna(raw)):
            return None
        if isinstance(raw, (int, float)):
            return float(raw) / 100.0
        s = str(raw).strip()
        if not s:
            return None
        if ";" in s and ":" in s:
            try:
                pairs = []
                for entry in s.split(";"):
                    d_str, r_str = entry.split(":")
                    d = self._parse_date(d_str.strip())
                    if d is not None:
                        pairs.append((d, float(r_str.strip()) / 100.0))
                pairs.sort()
                applicable = [r for d, r in pairs if d <= asof]
                return applicable[-1] if applicable else (pairs[0][1] if pairs else None)
            except (ValueError, TypeError):
                return None
        try:
            return float(s) / 100.0
        except ValueError:
            return None

    def _generate_bond_cashflows(self, row: pd.Series) -> List[Cashflow]:
        """Delegate al synth puro en core.domain.cashflow_synth.

        Antes este método tenía ~130 LOC con dispatch por tipo + lógica de
        amortizing/bullet/zero-coupon/step-up. Extraído a un módulo puro
        para que la ABM también lo use (vía `synth_cashflows`) sin tener que
        instanciar Repository.__new__.
        """
        from core.domain.cashflow_synth import synth_cashflows
        return synth_cashflows(row)

    def _load_cashflow_sheet(
        self,
        sheet_name: str,
        amort_col: str,
        interest_col: Optional[str],
        cf_map: Dict[str, List[Cashflow]],
        required: bool = True,
    ) -> int:
        """Append Cashflow rows from `sheet_name` to `cf_map`. Returns rows skipped."""
        try:
            df = pd.read_excel(self.excel_path, sheet_name=sheet_name)
        except (FileNotFoundError, ValueError, KeyError) as e:
            if required:
                raise
            logger.debug(f"{sheet_name} sheet not loaded: {e}")
            return 0

        skipped = 0
        for _, row in df.iterrows():
            t = str(row.get("ticker", "")).upper().strip()
            if not t or t == "NAN":
                continue
            cf_date = self._parse_date(row.get("fecha_pago"))
            if cf_date is None:
                skipped += 1
                continue
            cf_map.setdefault(t, []).append(Cashflow(
                date=cf_date,
                amortization=self._safe_float(row.get(amort_col, 0)),
                interest=self._safe_float(row.get(interest_col, 0)) if interest_col else 0.0,
            ))
        return skipped

    def _load_all(self):
        # Race-condition protection: la ABM escribe al mismo archivo Excel
        # vía instruments_abm._LOCK. Tomamos el MISMO lock acá para que un
        # reload del repo nunca coincida con un wb.save() en curso (que
        # podría dejar el archivo truncado mid-read). Import diferido para
        # evitar dependencia cíclica.
        try:
            from apps.web.instruments_abm import _LOCK as _ABM_LOCK
        except Exception:
            _ABM_LOCK = None  # tests / CLI sin la layer web cargada
        if _ABM_LOCK is not None:
            _ABM_LOCK.acquire()
        try:
            return self._load_all_impl()
        finally:
            if _ABM_LOCK is not None:
                _ABM_LOCK.release()

    def _load_all_impl(self):
        try:
            # Rows with no parseable date are skipped to avoid TypeError when
            # comparing cf.date >= reference downstream.
            cf_map: Dict[str, List[Cashflow]] = {}
            skipped = self._load_cashflow_sheet("Cashflows", "amortizacion", "cupon_interes", cf_map)
            skipped += self._load_cashflow_sheet("Cashflows_Fija", "monto", None, cf_map, required=False)

            if skipped:
                logger.warning(f"Skipped {skipped} cashflow rows with invalid fecha_pago.")

            self._cache_instruments = []
            xl = pd.ExcelFile(self.excel_path)
            sheet_names = [s for s in xl.sheet_names if s not in self.NON_INSTRUMENT_SHEETS]

            for sheet in sheet_names:
                try:
                    df = xl.parse(sheet)
                    df.columns = [str(c).lower().strip() for c in df.columns]
                    
                    for _, row in df.iterrows():
                        raw_ticker = None
                        for t_cand in ["ticker", "ticker_ref", "symbol"]:
                            if t_cand in row:
                                raw_ticker = str(row[t_cand]).upper().strip()
                                break
                        if not raw_ticker or raw_ticker in ("NAN", "NONE"):
                            continue
                        
                        # Excel `ticker` column is the source of truth — it must match what
                        # Data912 returns.
                        clean_ticker = raw_ticker
                        short = str(row.get("short_name", row.get("short name", raw_ticker)))
                        itype = str(row.get("tipo", row.get("clase", sheet))).upper().strip()
                        
                        # Load maturity date
                        m_date = None
                        for d_cand in ["fecha_vencimiento", "fecha vencimiento", "fecha_pago", "maturity"]:
                            if d_cand in row:
                                m_date = self._parse_date(row[d_cand])
                                if m_date: break

                        e_date = None
                        for d_cand in ["fecha_emision", "fecha emision"]:
                            if d_cand in row:
                                e_date = self._parse_date(row[d_cand])
                                if e_date: break
                        
                        # Link cashflows
                        cfs = cf_map.get(short.upper(), cf_map.get(raw_ticker, cf_map.get(clean_ticker, [])))
                        
                        # Dynamic Generation fallback
                        if not cfs and m_date:
                            cfs = self._generate_bond_cashflows(row)
                        
                        # Accept three column names: "cer emision" (CER sheet legacy),
                        # "cer_emision" (snake_case), "cer_base" (TAMAR sheet for TXMJ duals).
                        cer_b = self._safe_float(
                            row.get("cer emision",
                                    row.get("cer_emision",
                                            row.get("cer_base"))),
                            default=1.0,
                        )
                        lag_val = self._safe_int(row.get("dias habiles previos", row.get("dias_lag")), default=10)

                        cat_raw = row.get("categoria")
                        category = str(cat_raw).strip() if cat_raw is not None and not pd.isna(cat_raw) else None

                        floor_raw = row.get("tasa_fija_mensual") or row.get("tem_licit")
                        floor = float(floor_raw) if floor_raw is not None and not pd.isna(floor_raw) else None

                        # TAMAR spread (decimal, e.g. 0.05 for "TAMAR + 5%").
                        spread_raw = row.get("spread", row.get("spread_anual"))
                        spread = float(spread_raw) if spread_raw is not None and not pd.isna(spread_raw) else None

                        # CER spread for DUAL CER/TAMAR (TXMJ series).
                        cer_spread_raw = row.get("cer_spread", row.get("spread_cer"))
                        cer_spread_val = (
                            float(cer_spread_raw)
                            if cer_spread_raw is not None and not pd.isna(cer_spread_raw)
                            else None
                        )

                        # Payment frequency: prefer Excel column, fall back to
                        # inference from cashflows (median time-between-flows).
                        freq_raw = row.get("frecuencia pagos", row.get("frecuencia"))
                        freq = self._safe_int(freq_raw, default=0) if freq_raw is not None else 0
                        if freq <= 0:
                            freq = _infer_payment_frequency(cfs)

                        # Day-count convention. Acepta "base calculo" (con espacio, AR)
                        # o "base_calculo" (snake_case). Normaliza a forma canónica.
                        dc_raw = row.get("base calculo", row.get("base_calculo"))
                        if dc_raw is not None and not (isinstance(dc_raw, float) and pd.isna(dc_raw)):
                            dc_str = str(dc_raw).strip()
                        else:
                            dc_str = ""
                        # BOPREAL: 30/360 por prospecto BCRA aunque no esté en Excel.
                        if not dc_str and itype == "BOPREAL":
                            dc_str = "30/360"
                        day_count = dc_str if dc_str else "ACT/365.25"

                        self._cache_instruments.append(Instrument(
                            ticker=clean_ticker,
                            short_name=short,
                            instrument_type=itype,
                            maturity_date=m_date,
                            emission_date=e_date,
                            cashflows=cfs,
                            cer_base=cer_b,
                            cer_lag=lag_val,
                            category=category,
                            floor_rate_monthly=floor,
                            spread_rate=spread,
                            cer_spread=cer_spread_val,
                            payment_frequency=freq,
                            day_count=day_count,
                        ))
                except Exception as e:
                    logger.warning(f"Could not load sheet {sheet}: {e}")

            # Dedupe by ticker — later sheets override earlier ones. Prevents
            # double-counting when a bond appears in both Tasa_Fija and TAMAR.
            seen: Dict[str, Instrument] = {}
            for inst in self._cache_instruments:
                seen[inst.ticker] = inst
            self._cache_instruments = list(seen.values())
            self._by_ticker = seen

            by_type: Dict[str, List[Instrument]] = {}
            for inst in self._cache_instruments:
                by_type.setdefault(inst.instrument_type, []).append(inst)
            self._by_type = by_type

            logger.info(f"Repository loaded {len(self._cache_instruments)} unique instruments.")
        except Exception as e:
            logger.error(f"Error loading Excel repository: {e}")

    def get_all_instruments(self) -> List[Instrument]:
        return self._cache_instruments

    def get_instruments_by_type(self, instrument_type: str) -> List[Instrument]:
        return list(self._by_type.get(instrument_type, ()))

    def get_instrument_by_ticker(self, ticker: str) -> Optional[Instrument]:
        return self._by_ticker.get(ticker)

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
    _STOCK_HISTORY_TTL_S = 6 * 3600

    # Cache lives until explicit invalidation. The refresh loop calls
    # `invalidate_cache()` at the start of each cycle so every panel inside
    # the cycle reuses one network round-trip. A TTL alone wouldn't work:
    # when the cycle itself takes longer than the TTL, the cache expires
    # mid-cycle and every panel after that point re-hits the network (which
    # was the original bug). A long TTL as backstop catches CLI scripts and
    # other one-off callers that never invalidate.
    _CACHE_TTL_SEC = 60.0

    # Historical prices live on disk (CSV). Data912 has no historical endpoint;
    # the CSV is refreshed out-of-band. Tab-separated with one ticker per column.
    _HISTORY_CSV = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "history", "precio_historico.csv",
    )

    # Class-level so multiple provider instances share the historical cache
    # (the heavy startup cost is paid once per process).
    _stock_history_cache: Dict[str, "tuple[float, list]"] = {}
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
                payload = http_get_json(
                    url, timeout=self._FETCH_TIMEOUT_S, retries=self._FETCH_RETRIES,
                    user_agent=self.UA, source=f"Data912/{name}",
                )
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
            data = http_get_json(url, timeout=10, user_agent=self.UA,
                                 source=f"Data912/hist/{t}")
            if not isinstance(data, list):
                raise ValueError(f"expected list, got {type(data).__name__}")
            with self._stock_history_lock:
                self._stock_history_cache[t] = (time.monotonic(), data)
            return data
        except Exception as e:
            logger.warning(f"Stock history fetch {t} failed: {e}")
            return cached[1] if cached else []

    def fetch_historical_prices(self, ticker: str, days: int) -> Dict[date, float]:
        history = self._load_history()
        series = history.get(str(ticker).upper().strip())
        if not series or days <= 0:
            return series or {}
        cutoff = date.today() - timedelta(days=days)
        return {d: p for d, p in series.items() if d >= cutoff}
