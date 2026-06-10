import logging
import os
import threading
import time
import warnings

import pandas as pd
from typing import List, Dict, Optional
from datetime import date, datetime, timedelta
from core.domain.models import Instrument, Cashflow, MarketSnapshot
from core.domain.interfaces import IInstrumentsRepository, IMarketDataProvider
from core.infrastructure._http import http_get_json
from core.infrastructure.price_history import get_price_history_store

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


# --------------------------------------------------------------------------- #
# Parsing de fila → Instrument (compartido por el loader Excel y el ABM SQLite).
# Acepta cualquier mapping con `.get`/`in`: una pandas Series (loader) o un dict
# (form del ABM, claves ya en minúscula).
# --------------------------------------------------------------------------- #

def _isna(v) -> bool:
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _safe_int(val, default: int = 0) -> int:
    try:
        if pd.isna(val):
            return default
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _safe_float(val, default: float = 0.0) -> float:
    try:
        if pd.isna(val):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _parse_date_value(val) -> Optional[date]:
    """Parsea date de varios formatos sin warnings.

    Crítico: detectar ISO (YYYY-MM-DD) y parsear con dayfirst=False. pandas con
    dayfirst=True swapea mes/día en strings ISO (corrompía el schedule de cupones
    de los soberanos, que guardan fechas ISO en la hoja Cashflows)."""
    if val is None or _isna(val):
        return None
    if isinstance(val, pd.Timestamp):
        return val.date()
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    s = str(val).strip()
    if not s:
        return None
    iso_like = (len(s) >= 10 and s[4] == "-" and s[7] == "-" and s[:4].isdigit())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            dt = pd.to_datetime(s, dayfirst=not iso_like, errors="coerce")
            return dt.date() if pd.notna(dt) else None
        except (ValueError, TypeError):
            return None


def _resolve_ticker(row) -> Optional[str]:
    raw = None
    for cand in ("ticker", "ticker_ref", "symbol"):
        if cand in row:
            raw = str(row.get(cand)).upper().strip()
            break
    if not raw or raw in ("NAN", "NONE"):
        return None
    return raw


# Multi-ticker: un instrumento puede cotizar en hasta 3 monedas con tickers
# distintos (mismo bono, mismos flujos): pesos (ARS) / MEP (sufijo D) / CABLE (C).
_CCY_TICKER_COLS = ("ticker_ars", "ticker_mep", "ticker_ccl")


def _currency_tickers(row) -> List[str]:
    """Tickers de un instrumento (1-3), en orden de slot de moneda. Soporta el
    esquema nuevo (ticker_ars/ticker_mep/ticker_ccl) y el viejo (`ticker` único).
    El primero es el primario (PK / clave de cashflows)."""
    out: List[str] = []
    for col in _CCY_TICKER_COLS:
        if col in row:
            v = row.get(col)
            if v is not None and not _isna(v):
                t = str(v).upper().strip()
                if t and t not in ("NAN", "NONE") and t not in out:
                    out.append(t)
    if out:
        return out
    t = _resolve_ticker(row)
    return [t] if t else []


def expand_currency_legs(primary: Instrument, secondary_tickers) -> List[Instrument]:
    """Una especie (Instrument) por ticker: primario + secundarios, compartiendo
    términos y flujos. Cada especie linkea con su precio de Data912 por su propio
    ticker; la moneda la deriva el motor del sufijo (D=MEP, C=CABLE)."""
    legs = [primary]
    seen = {primary.ticker}
    for t in secondary_tickers or ():
        tu = str(t).upper().strip()
        if tu and tu not in seen:
            legs.append(primary.model_copy(update={"ticker": tu}))
            seen.add(tu)
    return legs


def split_currency_tickers(tickers: List[str]):
    """[primario, *secundarios] → (primario, ticker_mep, ticker_ccl) para storage.
    Los secundarios se asignan por sufijo (D→mep, C→ccl); sin sufijo van a la
    primera ranura libre. Solo para que las columnas queden semánticas; la
    expansión y el pricing usan el sufijo, no la columna."""
    primary = tickers[0]
    mep = ccl = None
    for t in tickers[1:]:
        tu = str(t).upper().strip()
        if not tu:
            continue
        if tu.endswith("D") and not mep:
            mep = tu
        elif tu.endswith("C") and not ccl:
            ccl = tu
        elif not mep:
            mep = tu
        elif not ccl:
            ccl = tu
    return primary, mep, ccl


def _first_present(row, keys):
    """Valor de la primera clave que EXISTE (aunque su valor sea None/NaN) —
    replica la semántica de `row.get(a, row.get(b, ...))`."""
    for k in keys:
        if k in row:
            return row.get(k)
    return None


def _first_date(row, candidates) -> Optional[date]:
    for c in candidates:
        if c in row:
            d = _parse_date_value(row.get(c))
            if d:
                return d
    return None


def build_instrument(row, sheet: str, cashflows: List[Cashflow]) -> Optional[Instrument]:
    """Construye el `Instrument` PRIMARIO desde una fila (mapping de columnas en
    minúscula) + hoja + cashflows. Devuelve None si no hay ticker. Las patas de
    moneda secundarias se expanden aparte (`expand_currency_legs`)."""
    tickers = _currency_tickers(row)
    if not tickers:
        return None
    raw_ticker = tickers[0]
    short = str(row.get("short_name", row.get("short name", raw_ticker)))
    itype = str(row.get("tipo", row.get("clase", sheet))).upper().strip()
    m_date = _first_date(row, ("fecha_vencimiento", "fecha vencimiento", "fecha_pago", "maturity"))
    e_date = _first_date(row, ("fecha_emision", "fecha emision"))

    # cer base: "cer emision" (legacy) / "cer_emision" (snake) / "cer_base" (TAMAR duals).
    cer_b = _safe_float(_first_present(row, ("cer emision", "cer_emision", "cer_base")), default=1.0)
    lag_val = _safe_int(_first_present(row, ("dias habiles previos", "dias_lag")), default=10)

    cat_raw = row.get("categoria")
    category = str(cat_raw).strip() if (cat_raw is not None and not _isna(cat_raw)) else None
    if category is None and itype in ("HARD DOLLAR", "DOLLAR LINKED"):  # categoría fija de las ONs
        category = "Obligaciones Negociables"

    floor_raw = row.get("tasa_fija_mensual") or row.get("tem_licit")
    floor = float(floor_raw) if (floor_raw is not None and not _isna(floor_raw)) else None

    spread_raw = _first_present(row, ("spread", "spread_anual"))
    spread = float(spread_raw) if (spread_raw is not None and not _isna(spread_raw)) else None

    cer_spread_raw = _first_present(row, ("cer_spread", "spread_cer"))
    cer_spread_val = float(cer_spread_raw) if (cer_spread_raw is not None and not _isna(cer_spread_raw)) else None

    freq_raw = _first_present(row, ("frecuencia pagos", "frecuencia"))
    freq = _safe_int(freq_raw, default=0) if freq_raw is not None else 0
    if freq <= 0:
        freq = _infer_payment_frequency(cashflows)

    dc_raw = _first_present(row, ("base calculo", "base_calculo"))
    dc_str = str(dc_raw).strip() if (dc_raw is not None and not _isna(dc_raw)) else ""
    if not dc_str and itype == "BOPREAL":          # 30/360 por prospecto BCRA
        dc_str = "30/360"
    elif not dc_str and itype in ("HARD DOLLAR", "DOLLAR LINKED"):  # ON: real/365 (informe)
        dc_str = "ACT/365"
    day_count = dc_str or "ACT/365.25"

    ley_raw = _first_present(row, ("ley_aplicable", "ley aplicable", "ley"))
    ley = str(ley_raw).strip() if (ley_raw is not None and not _isna(ley_raw)) else None

    isin_raw = _first_present(row, ("isin", "codigoisin", "codigo_isin"))
    isin = str(isin_raw).strip().upper() if (isin_raw is not None and not _isna(isin_raw)) else None

    return Instrument(
        ticker=raw_ticker, short_name=short, instrument_type=itype,
        maturity_date=m_date, emission_date=e_date, cashflows=cashflows,
        cer_base=cer_b, cer_lag=lag_val, category=category,
        floor_rate_monthly=floor, spread_rate=spread, cer_spread=cer_spread_val,
        payment_frequency=freq, day_count=day_count, ley_aplicable=ley or None,
        isin=isin or None,
    )


def _json_scalar(v):
    """Coerce un valor de celda a un escalar JSON-serializable (para raw_fields)."""
    if v is None or _isna(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if hasattr(v, "item"):  # escalares numpy
        v = v.item()
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def row_to_raw_fields(row) -> Dict[str, object]:
    """Serializa una fila (Series/dict, claves en minúscula) a un dict JSON-safe."""
    items = row.items() if hasattr(row, "items") else []
    return {str(k).lower().strip(): _json_scalar(v) for k, v in items}


class ExcelInstrumentsRepository(IInstrumentsRepository):
    NON_INSTRUMENT_SHEETS = frozenset({"Cashflows", "Cashflows_Fija", "Metadata", "Cotizaciones"})

    def __init__(self, excel_path: str):
        self.excel_path = excel_path
        self._cache_instruments: List[Instrument] = []
        self._by_ticker: Dict[str, Instrument] = {}
        self._by_type: Dict[str, List[Instrument]] = {}
        # ticker -> (sheet, raw_fields) para el seeding del ABM (round-trip del form).
        self._meta: Dict[str, tuple] = {}
        self._load_all()

    # Delegaciones a los helpers módulo (compartidos con build_instrument / ABM).
    def _parse_date(self, val) -> Optional[date]:
        return _parse_date_value(val)

    @staticmethod
    def _safe_int(val, default=0):
        return _safe_int(val, default)

    @staticmethod
    def _safe_float(val, default=0.0):
        return _safe_float(val, default)

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
        # El Excel ya es solo semilla: la ABM escribe SQLite, no toca el master.
        # Por eso ya no hace falta compartir lock con instruments_abm.
        return self._load_all_impl()

    def _load_all_impl(self):
        try:
            # Rows with no parseable date are skipped to avoid TypeError when
            # comparing cf.date >= reference downstream.
            cf_map: Dict[str, List[Cashflow]] = {}
            skipped = self._load_cashflow_sheet("Cashflows", "amortizacion", "cupon_interes", cf_map)
            skipped += self._load_cashflow_sheet("Cashflows_Fija", "monto", None, cf_map, required=False)

            if skipped:
                logger.warning(f"Skipped {skipped} cashflow rows with invalid fecha_pago.")

            # bonos[primary_ticker] = (primary_inst, sheet, raw_fields, secondary_tickers)
            # Una entrada por BONO (no por pata de moneda); hojas posteriores pisan.
            bonos: Dict[str, tuple] = {}
            xl = pd.ExcelFile(self.excel_path)
            sheet_names = [s for s in xl.sheet_names if s not in self.NON_INSTRUMENT_SHEETS]

            for sheet in sheet_names:
                try:
                    df = xl.parse(sheet)
                    df.columns = [str(c).lower().strip() for c in df.columns]

                    for _, row in df.iterrows():
                        tickers = _currency_tickers(row)
                        if not tickers:
                            continue
                        primary, secondaries = tickers[0], tickers[1:]
                        short = str(row.get("short_name", row.get("short name", primary)))
                        m_date = _first_date(row, ("fecha_vencimiento", "fecha vencimiento",
                                                   "fecha_pago", "maturity"))

                        # Cashflows (hoja Cashflows por short/primario) o synth fallback.
                        cfs = cf_map.get(short.upper(), cf_map.get(primary, []))
                        if not cfs and m_date:
                            cfs = self._generate_bond_cashflows(row)

                        inst = build_instrument(row, sheet, cfs)
                        if inst is None:
                            continue
                        bonos[primary] = (inst, sheet, row_to_raw_fields(row), secondaries)
                except Exception as e:
                    logger.warning(f"Could not load sheet {sheet}: {e}")

            # Expandir cada bono a una especie por ticker (pricing/paneles las ven
            # como instrumentos independientes, c/u con su precio de Data912).
            self._bonos = list(bonos.values())
            self._cache_instruments = []
            self._meta = {}
            for inst, sheet, raw, secondaries in self._bonos:
                for leg in expand_currency_legs(inst, secondaries):
                    self._cache_instruments.append(leg)
                    self._meta[leg.ticker] = (sheet, raw)

            self._by_ticker = {i.ticker: i for i in self._cache_instruments}
            by_type: Dict[str, List[Instrument]] = {}
            for inst in self._cache_instruments:
                by_type.setdefault(inst.instrument_type, []).append(inst)
            self._by_type = by_type

            logger.info(f"Repository loaded {len(self._cache_instruments)} unique instruments "
                        f"({len(self._bonos)} bonos).")
        except Exception as e:
            logger.error(f"Error loading Excel repository: {e}")

    def get_all_instruments(self) -> List[Instrument]:
        return self._cache_instruments

    def get_instruments_by_type(self, instrument_type: str) -> List[Instrument]:
        return list(self._by_type.get(instrument_type, ()))

    def get_instrument_by_ticker(self, ticker: str) -> Optional[Instrument]:
        return self._by_ticker.get(ticker)

    def get_all_with_meta(self):
        """[(primary_inst, sheet, raw_fields, secondary_tickers), ...] — UNA entrada
        por bono (no por pata) para el seeding 1-fila-por-instrumento de SQLite."""
        return list(getattr(self, "_bonos", []))

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
            data = http_get_json(url, timeout=10, user_agent=self.UA,
                                 source=f"Data912/histbond/{t}")
            if not isinstance(data, list):  # {} (no cubierto) → tratar como vacío
                data = []
            with self._stock_history_lock:
                self._bond_history_cache[t] = (time.monotonic(), data)
            return data
        except Exception as e:
            logger.warning(f"Bond history fetch {t} failed: {e}")
            return cached[1] if cached else []

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
