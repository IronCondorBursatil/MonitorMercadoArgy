import copy
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone, date, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

from apps.web import bond_detail, cartera_store, instruments_abm
from config.settings import MASTER_XLSX, setup_logging
from core.domain import portfolio as portfolio_engine, scenarios as scenario_engine
from core.domain.instrument_groups import (
    BOPREALES, CER, DOLAR_LINKED, DUAL_TAMAR, PANEL_LIDER, SOBERANOS, TAMAR, TASA_FIJA,
)
from core.infrastructure.repositories import ExcelInstrumentsRepository, Data912MarketDataProvider
from core.use_cases.generate_report import GenerateMonitorReport

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(THIS_DIR, "static")

REFRESH_SEC     = 5
BEI_REFRESH_SEC = 300     # BEI is heavy (curve fit + REM); 5 min is plenty.
HOST = "127.0.0.1"
PORT = 8000

# Financial calculation constants
_DV01_BP         = 0.0001       # 1 basis point; DV01 = MD × Price × _DV01_BP
_ONE_MONTH_YEARS = 1.0 / 12.0   # roll-down horizon (30d ≈ 1/12 year)

# Shutdown signaling: los background threads (refresh + BEI) son daemon, así
# que Python los marca para morir en Ctrl+C pero pueden quedar mid-execution.
# Si en ese momento usan un ThreadPoolExecutor, submit() tira
# RuntimeError("cannot schedule new futures after interpreter shutdown") y
# ensucia el log. Con este Event, ambos loops chequean antes de cada iter
# y reemplazan time.sleep() por Event.wait() (sale temprano al set()).
_SHUTDOWN_EVENT = threading.Event()


def _is_shutdown_error(exc: Exception) -> bool:
    """Reconoce errores transientes del interpreter en shutdown (race entre
    daemon threads y main thread). No son bugs, son shutdown noise."""
    msg = str(exc)
    return isinstance(exc, RuntimeError) and "interpreter shutdown" in msg

# {csv_path: (mtime, serialized_json_bytes)} — invalidated when the file is rewritten.
# Lock para evitar TOCTOU al check+set entre múltiples requests HTTP concurrentes
# (ThreadingHTTPServer = N hilos). En CPython el GIL hace que dict assignment
# sea atómico, pero la secuencia check-modify-set no — sin lock dos hilos
# podrían parsear el mismo CSV en paralelo (cache miss simultáneo).
_BEI_HISTORY_CACHE: dict = {}
_BEI_HISTORY_LOCK = threading.Lock()

# Histórico de bonos vía data912 (OHLC + dr/sa). Rate limit 120/min upstream,
# por eso cacheamos por ~10min: la serie es diaria, no se mueve intraday.
_BOND_HISTORY_CACHE: dict = {}   # {ticker: (timestamp, payload_list)}
_BOND_HISTORY_LOCK = threading.Lock()
_BOND_HISTORY_TTL_S = 600
_BOND_HISTORY_BASE = "https://data912.com/historical/bonds"
_BOND_HISTORY_TICKER_RE = re.compile(r"^[A-Za-z0-9]{2,12}$")

_STOCK_HISTORY_CACHE: dict = {}
_STOCK_HISTORY_LOCK = threading.Lock()
_STOCK_HISTORY_TTL_S = 300   # 5 min — series diaria, estable intraday
_STOCK_HISTORY_BASE = "https://data912.com/historical/stocks"
_STOCK_HISTORY_TICKER_RE = re.compile(r"^[A-Za-z0-9]{2,8}$")

STOCK_HISTORY_SUPPORTED_TICKERS = frozenset({
    "ALUA", "BBAR", "BMA", "BYMA", "CEPU", "COME", "CRES", "CVH", "EDN",
    "GGAL", "LOMA", "MIRG", "PAMP", "SUPV", "TECO2", "TGNO4", "TGSU2",
    "TRAN", "TXAR", "VALO", "YPFD",
    "AGRO", "AUSO", "BHIP", "BOLT", "BPAT", "CADO", "CAPX", "CARC",
    "CECO2", "CELU", "CGPA2", "CTIO", "DGCU2", "DOME", "DYCA", "FERR",
    "FIPL", "GAMI", "GARO", "GBAN", "GCDI", "GCLA", "GRIM", "HARG",
    "HAVA", "HSAT", "INTR", "INVJ", "IRSA", "LEDE", "LONG", "METR",
    "MOLA", "MOLI", "MORI", "MTR", "OEST", "PATA", "POLL", "RICH",
    "RIGO", "ROSE", "SAMI", "SEMI",
})
# Bond detail/calculate accept optional _TF / _TAM / _CER leg suffixes for
# dual-leg bonds (e.g. TTJ26_TF, TTJ26_TAM).
_BOND_DETAIL_TICKER_RE = re.compile(r"^[A-Za-z0-9]{2,12}(?:_(?:TF|TAM|CER))?$")

# Rate limit simple (token bucket) para /api/abm/preview_cashflows. Sin esto,
# un cliente malicioso puede mandar 10k req/s y saturar CPU del thread pool.
# Aceptamos 120 req/min globalmente (≥2/s) — cómodo para uso humano normal.
_PREVIEW_RATE_WINDOW_S = 60.0
_PREVIEW_RATE_MAX     = 120   # requests/minute
_PREVIEW_RATE_BUCKET: deque = deque()
_PREVIEW_RATE_LOCK = threading.Lock()


def _preview_rate_allow() -> bool:
    """True si el request entra dentro del rate limit, False si fue rechazado."""
    now = time.monotonic()
    with _PREVIEW_RATE_LOCK:
        # Purgar tokens viejos (>1 ventana).
        while _PREVIEW_RATE_BUCKET and (now - _PREVIEW_RATE_BUCKET[0]) > _PREVIEW_RATE_WINDOW_S:
            _PREVIEW_RATE_BUCKET.popleft()
        if len(_PREVIEW_RATE_BUCKET) >= _PREVIEW_RATE_MAX:
            return False
        _PREVIEW_RATE_BUCKET.append(now)
        return True

# Universo de tickers que data912.com/historical/bonds soporta. Hardcoded
# porque el endpoint no expone un /list. KEEP IN SYNC con la misma constante
# en apps/web/static/app.js (HISTORY_SUPPORTED_TICKERS): si se actualiza una,
# actualizar la otra. Lo usamos para:
#   1) Avisar al boot qué tickers están en data912 pero NO en el master Excel.
#   2) Rechazar tempranamente con 400 los requests a /api/bond_history/<ticker>
#      no soportados (sino el frontend espera ~4s para un 502 del upstream).
HISTORICAL_SUPPORTED_TICKERS = frozenset({
    "AE38", "AE38D",
    "AL29", "AL29D",
    "AL30", "AL30C", "AL30D",
    "AL35", "AL35D",
    "AL41", "AL41D",
    "BA37D", "BB37D", "BC37D", "BDC28", "BPY26",
    "CO26", "CO26D",
    "CUAP", "DICP", "DIP0",
    "GD29", "GD29D",
    "GD30", "GD30C", "GD30D",
    "GD35", "GD35D",
    "GD38", "GD38D",
    "GD41", "GD41D",
    "GD46", "GD46D",
    "NDT25", "PAP0", "PARP", "PBA25",
    "T2X5", "TDE25", "TO26",
    "TVPA", "TVPE", "TVPP", "TVPY",
    "TX25", "TX26", "TX28",
})

setup_logging()
logger = logging.getLogger("monitor_web")


def _safe_lag(val) -> int:
    """Parse settlement_lag from query string / JSON body. Clamp a {0, 1}
    para evitar lags absurdos que disparen settlement_byma a 30 días."""
    try:
        n = int(float(val))
    except (TypeError, ValueError):
        return 1
    return 0 if n <= 0 else 1

# --------------------------------------------------------------------------- #
# Helpers JSON
# --------------------------------------------------------------------------- #
def _json_default(obj) -> Any:
    if isinstance(obj, (date, datetime)):
        return obj.strftime("%Y-%m-%d")
    if isinstance(obj, pd.Timestamp):
        return obj.strftime("%Y-%m-%d")
    if isinstance(obj, (np.floating,)):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return str(obj)


def _safe_num(v) -> Optional[float]:
    """Sanitize a float for JSON: NaN/inf → 0. Prevents invalid JSON output."""
    if v is None:
        return None
    try:
        f = float(v)
        return 0.0 if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None

# --------------------------------------------------------------------------- #
# Columns Definitions
# --------------------------------------------------------------------------- #
def _get_columns(monitor_id: str):
    bonares_cols = [
        {"key": "ticker", "label": "Ticker", "kind": "text"},
        {"key": "vto", "label": "Vto", "kind": "date"},
        {"key": "days_next_coupon", "label": "Próx Cup", "kind": "number", "decimals": 0},
        {"key": "price", "label": "Precio", "kind": "number", "decimals": 2},
        {"key": "technical_value", "label": "V.Téc", "kind": "number", "decimals": 2},
        {"key": "parity", "label": "Paridad", "kind": "percent", "decimals": 2},
        {"key": "tir", "label": "TIR", "kind": "percent", "decimals": 2},
        {"key": "duration", "label": "MD", "kind": "number", "decimals": 2},
        {"key": "change_pct", "label": "%Día", "kind": "percent_signed", "decimals": 2},
        {"key": "volume", "label": "Vol $", "kind": "volume"},
        # Rich-cheap — activables desde ⚙ Layout
        {"key": "spread_curva", "label": "vs curva", "kind": "percent_signed", "decimals": 2},
        {"key": "carry_roll", "label": "C+R 30d", "kind": "percent_signed", "decimals": 2},
    ]
    schemas = {
        "bonares": bonares_cols,
        "bopreales": bonares_cols,
        "dolar_linked": bonares_cols,
        "cer": [
            {"key": "ticker", "label": "Ticker", "kind": "text"},
            {"key": "category", "label": "Categoría", "kind": "text"},
            {"key": "vto", "label": "Vto", "kind": "date"},
            {"key": "days_next_coupon", "label": "Próx Cup", "kind": "number", "decimals": 0},
            {"key": "price", "label": "Precio", "kind": "number", "decimals": 2},
            {"key": "technical_value", "label": "V.Téc", "kind": "number", "decimals": 2},
            {"key": "parity", "label": "Paridad", "kind": "percent", "decimals": 2},
            {"key": "tir", "label": "TIR", "kind": "percent", "decimals": 2},
            {"key": "duration", "label": "DM", "kind": "number", "decimals": 2},
            {"key": "change_pct", "label": "Var%", "kind": "percent_signed", "decimals": 2},
            {"key": "volume", "label": "Vol $", "kind": "volume"},
            {"key": "spread_curva", "label": "vs curva", "kind": "percent_signed", "decimals": 2},
            {"key": "carry_roll", "label": "C+R 30d", "kind": "percent_signed", "decimals": 2},
        ],
        "tasa_fija": [
            {"key": "ticker",      "label": "Ticker",    "kind": "text"},
            {"key": "dias",        "label": "Días",      "kind": "number",         "decimals": 0},
            {"key": "price",       "label": "Precio",    "kind": "number",         "decimals": 2},
            {"key": "technical_value", "label": "V.Téc", "kind": "number",         "decimals": 2},
            {"key": "parity",      "label": "Paridad",   "kind": "percent",        "decimals": 2},
            {"key": "tir",         "label": "TIR/TEA",  "kind": "percent",        "decimals": 2},
            {"key": "tna",         "label": "TNA(365)",  "kind": "percent",        "decimals": 2},
            {"key": "tem",         "label": "TEM(365)",  "kind": "percent",        "decimals": 2},
            # Columnas base-360 (Secretaría de Finanzas): ocultas por default, activables desde ▦
            {"key": "tna_360",     "label": "TNA(360)",  "kind": "percent",        "decimals": 2},
            {"key": "tem_360",     "label": "TEM(360)",  "kind": "percent",        "decimals": 2},
            {"key": "duration",    "label": "DM",        "kind": "number",         "decimals": 2},
            {"key": "change_pct",  "label": "Var %",     "kind": "percent_signed", "decimals": 2},
            {"key": "volume",      "label": "Vol $",     "kind": "volume"},
            # Métricas avanzadas — ocultas por default, activables desde ▦
            {"key": "dv01",        "label": "DV01",      "kind": "number",         "decimals": 4},
            {"key": "convexity",   "label": "Convex.",   "kind": "number",         "decimals": 2},
            {"key": "tir_real",    "label": "TIR real",  "kind": "percent_signed", "decimals": 2},
            {"key": "carry_roll",  "label": "C+R 30d",   "kind": "percent_signed", "decimals": 2},
            {"key": "spread_curva","label": "Spread c.", "kind": "percent_signed", "decimals": 2},
        ],
        "tamar": [
            {"key": "ticker", "label": "Ticker", "kind": "text"},
            {"key": "vto", "label": "Vto", "kind": "date"},
            {"key": "dias", "label": "Días", "kind": "number", "decimals": 0},
            {"key": "price", "label": "Precio", "kind": "number", "decimals": 2},
            {"key": "tir", "label": "TIR (TEA)", "kind": "percent", "decimals": 2},
            {"key": "duration", "label": "MD", "kind": "number", "decimals": 2},
            {"key": "change_pct", "label": "%Día", "kind": "percent_signed", "decimals": 2},
            {"key": "volume", "label": "Vol $", "kind": "volume"},
            {"key": "spread_curva", "label": "vs curva", "kind": "percent_signed", "decimals": 2},
            {"key": "carry_roll", "label": "C+R 30d", "kind": "percent_signed", "decimals": 2},
        ],
        "futuros": [
            {"key": "ticker", "label": "Contrato", "kind": "text"},
            {"key": "vto", "label": "Vto", "kind": "date"},
            {"key": "bid", "label": "Compra", "kind": "number", "decimals": 2},
            {"key": "ask", "label": "Venta", "kind": "number", "decimals": 2},
            {"key": "last", "label": "Último", "kind": "number", "decimals": 2},
            {"key": "settle", "label": "Ajuste", "kind": "number", "decimals": 2},
            {"key": "tna", "label": "TNA", "kind": "percent", "decimals": 2},
            {"key": "open_interest", "label": "OP.INT", "kind": "volume"},
            {"key": "volume", "label": "Vol", "kind": "volume"},
        ],
        "panel_lider": [
            {"key": "ticker", "label": "Ticker", "kind": "text"},
            {"key": "bid", "label": "Compra", "kind": "number", "decimals": 2},
            {"key": "ask", "label": "Venta", "kind": "number", "decimals": 2},
            {"key": "mid", "label": "Mid", "kind": "number", "decimals": 2},
            {"key": "change_pct", "label": "Día %", "kind": "percent_bullet", "decimals": 2},
            {"key": "change_5d", "label": "5 ruedas %", "kind": "percent_bullet", "decimals": 2},
            {"key": "change_30d", "label": "30 días %", "kind": "percent_bullet", "decimals": 2},
            {"key": "sparkline", "label": "30d", "kind": "sparkline_range"},
            {"key": "volume", "label": "Vol $", "kind": "volume"},
            {"key": "operations", "label": "Ops", "kind": "number", "decimals": 0},
        ],
        "bei_tenor": [
            {"key": "plazo", "label": "Plazo", "kind": "text"},
            {"key": "dias", "label": "Días", "kind": "number", "decimals": 0},
            {"key": "tea_nominal", "label": "TEA Nom", "kind": "percent", "decimals": 2},
            {"key": "tea_real", "label": "TEA Real", "kind": "percent", "decimals": 2},
            {"key": "tamar_fwd", "label": "TAMAR fwd", "kind": "percent", "decimals": 2},
            {"key": "bei_spot", "label": "BEI spot", "kind": "percent", "decimals": 2},
            {"key": "bei_fwd", "label": "BEI fwd", "kind": "percent", "decimals": 2},
            {"key": "bei_g_adj", "label": "BEI γ-adj", "kind": "percent", "decimals": 2},
            {"key": "bei_tamar", "label": "BEI TAMAR", "kind": "percent", "decimals": 2},
            {"key": "dev_implicita", "label": "Deval DLR", "kind": "percent", "decimals": 2},
            {"key": "tc_real", "label": "TC real", "kind": "percent_signed", "decimals": 2},
        ],
        "bei_sendero": [
            {"key": "mes", "label": "Mes", "kind": "text"},
            {"key": "dias_mes", "label": "Días", "kind": "number", "decimals": 0},
            {"key": "bei_mensual", "label": "BEI mensual", "kind": "percent", "decimals": 2},
            {"key": "rem_mensual", "label": "REM mensual", "kind": "percent", "decimals": 2},
            {"key": "diff", "label": "BEI − REM", "kind": "percent_signed", "decimals": 2},
        ],
        "bei_pares": [
            {"key": "lecap", "label": "LECAP", "kind": "text"},
            {"key": "boncer", "label": "BONCER", "kind": "text"},
            {"key": "vto_lecap", "label": "Vto LECAP", "kind": "date"},
            {"key": "vto_cer", "label": "Vto CER", "kind": "date"},
            {"key": "dias", "label": "Días", "kind": "number", "decimals": 0},
            {"key": "delta_m1", "label": "δ − 1", "kind": "percent", "decimals": 2},
            {"key": "infl_mensual_impl", "label": "Infl mes impl.", "kind": "percent", "decimals": 2},
        ],
        "valor_relativo": [
            {"key": "ticker", "label": "Ticker", "kind": "text"},
            {"key": "grupo", "label": "Tipo", "kind": "text"},
            {"key": "duration", "label": "MD", "kind": "number", "decimals": 2},
            {"key": "tir", "label": "TIR", "kind": "percent", "decimals": 2},
            {"key": "spread_curva", "label": "vs curva", "kind": "percent_signed", "decimals": 2},
            {"key": "carry_roll", "label": "C+R 30d", "kind": "percent_signed", "decimals": 2},
        ],
    }
    return schemas.get(monitor_id, [])

# --------------------------------------------------------------------------- #
# Snapshot & Loop
# --------------------------------------------------------------------------- #
class Snapshot:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {
            "ts": None,
            "fx": {},
            "monitors": [
                {"id": "bonares", "title": "BONARES Y GLOBALES", "status": "loading", "rows": [], "columns": _get_columns("bonares")},
                {"id": "bopreales", "title": "BOPREALES", "status": "loading", "rows": [], "columns": _get_columns("bopreales")},
                {"id": "cer", "title": "BONOS CER", "status": "loading", "rows": [], "columns": _get_columns("cer")},
                {"id": "tasa_fija", "title": "TASA FIJA", "status": "loading", "rows": [], "columns": _get_columns("tasa_fija")},
                {"id": "dolar_linked", "title": "DOLAR LINKED", "status": "loading", "rows": [], "columns": _get_columns("dolar_linked")},
                {"id": "tamar", "title": "TAMAR (PURO)", "status": "loading", "rows": [], "columns": _get_columns("tamar")},
                {"id": "futuros", "title": "FUTUROS ROFEX", "status": "loading", "rows": [], "columns": _get_columns("futuros")},
                {"id": "panel_lider", "title": "PANEL LÍDER (Acciones)", "status": "loading", "rows": [], "columns": _get_columns("panel_lider")},
                {"id": "bei_tenor", "title": "BEI POR TENOR (NSS + Fisher)", "status": "loading", "rows": [], "columns": _get_columns("bei_tenor")},
                {"id": "bei_sendero", "title": "SENDERO MENSUAL · BEI vs REM-BCRA", "status": "loading", "rows": [], "columns": _get_columns("bei_sendero")},
                {"id": "bei_pares", "title": "MÉTODO DE PARES (cross-check NT8 §A)", "status": "loading", "rows": [], "columns": _get_columns("bei_pares")},
                {"id": "valor_relativo", "title": "VALOR RELATIVO · rich / cheap", "status": "loading", "rows": [], "columns": _get_columns("valor_relativo")},
            ],
        }

    def get(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def update_monitor(self, mid, **fields):
        with self._lock:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for m in self._data["monitors"]:
                if m["id"] == mid:
                    m.update(fields)
                    m["ts"] = now
                    break
            self._data["ts"] = now

    def update_fx(self, fx_dict):
        with self._lock:
            self._data["fx"] = fx_dict
            self._data["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

def _scale_pct(v: Optional[float]) -> Optional[float]:
    """Backend returns TIR/variance as decimals (0.0131 = 1.31%). The JS
    formatter just appends a "%", so scale to percentage units here.
    Note: `change_pct` from Data912 is already in % units — do NOT pass it through this.
    """
    return v * 100 if v is not None else None


def _next_coupon_date(inst, today: date) -> Optional[date]:
    """Date of the next coupon payment (future cashflow with interest > 0).
    Returns None for zero-coupon / discount instruments (LECER, LECAP,
    BONCER ZC, etc.) where the only cashflow is principal."""
    for cf in sorted(inst.cashflows or [], key=lambda c: c.date):
        if cf.date > today and cf.interest > 0:
            return cf.date
    return None


def _by_md_asc(metrics: list) -> list:
    """Sort metrics by Modified Duration ascending; None goes last."""
    return sorted(metrics, key=lambda m: (m.duration is None, m.duration or 0.0))


def _parse_history_dates(history: list) -> List[tuple]:
    """Return [(date, close)] for all valid OHLC records, sorted ascending."""
    out: List[tuple] = []
    for rec in (history or []):
        try:
            d = datetime.strptime(rec.get("date", ""), "%Y-%m-%d").date()
            c = rec.get("c")
            if c is not None:
                out.append((d, float(c)))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def _close_n_trading_days_ago(history: list, n: int) -> Optional[float]:
    """Close price `n` trading records before today (skips weekends/holidays
    naturally because Data912 only stores trading-day rows)."""
    today = date.today()
    seen = 0
    for d, c in reversed(_parse_history_dates(history)):
        if d >= today:
            continue
        seen += 1
        if seen >= n:
            return c
    return None


def _close_at_calendar_days_ago(history: list, n: int) -> Optional[float]:
    """Most recent close on or before (today − n calendar days)."""
    target = date.today() - timedelta(days=n)
    best: Optional[tuple] = None
    for d, c in _parse_history_dates(history):
        if d <= target and (best is None or d > best[0]):
            best = (d, c)
    return best[1] if best else None


def _pct_change(now: Optional[float], then: Optional[float]) -> Optional[float]:
    """Return % change as a percentage unit (e.g. 5.2 for +5.2%)."""
    if now is None or not then:
        return None
    return (now / then - 1.0) * 100.0


def _base_bond_row(m, *, today: date, include_dias: bool = False,
                   include_category: bool = False, extra: Optional[dict] = None) -> dict:
    """Common row shape for bond-style monitors (bonares, bopreales, dl, cer, tamar, dual, tasa_fija)."""
    from core.domain.services import FinancialEngine
    inst = m.snapshot.instrument
    vto = inst.maturity_date
    next_cp = _next_coupon_date(inst, today)
    # Parity stored as decimal (0.635 = 63.5%); _scale_pct turns it into "63.5".
    # Two TNA/TEM conventions: base-365 (diaria, act/365) y base-360 (mensual m=12,
    # convención oficial Secretaría de Finanzas para LECAPs/BONCAPs).
    row = {
        "ticker": inst.ticker,
        "vto": vto,
        "next_coupon_date": next_cp,
        "days_next_coupon": (next_cp - today).days if next_cp else None,
        "price": m.snapshot.price,
        "technical_value": m.technical_value,
        "parity": _scale_pct(m.parity),
        "tir": _scale_pct(m.tir),
        "tna": _scale_pct(FinancialEngine.tea_to_tna(m.tir)),          # base-365 diaria
        "tem": _scale_pct(FinancialEngine.tea_to_tem(m.tir)),          # act/365 (30d)
        "tna_360": _scale_pct(FinancialEngine.tea_to_tna_monthly(m.tir)),  # m=12
        "tem_360": _scale_pct(FinancialEngine.tea_to_tem_m12(m.tir)),  # (1+TEA)^(1/12)-1
        "duration": m.duration,
        "change_pct": m.snapshot.change_pct,
        "volume": m.snapshot.volume,
    }
    if include_category:
        row["category"] = inst.category
    if include_dias:
        row["dias"] = (vto - today).days if vto else 0
    if extra:
        row.update(extra)
    return row


# --------------------------------------------------------------------------- #
# _refresh_loop — descompuesto en orquestador + funciones por panel.
# Antes era un solo método de ~300 LOC con 8 responsabilidades mezcladas
# (cyclomatic ≈ 25). Ahora el loop es solo el orquestador del adaptive
# sleep + summary, y cada panel es una función testeable en isolation.
# --------------------------------------------------------------------------- #

@dataclass
class _RefreshContext:
    """Bundle de dependencias del refresh loop — instanciado una vez al boot."""
    repo: object
    provider: object
    use_case: object
    fx: object
    rofex: object
    bcra: object
    rem: object
    rofex_symbols: List[str]
    parse_contract_maturity: object
    implied_tna: object
    resolve_spot_for_tna: object
    bond_panels: Tuple[tuple, ...]
    all_bond_types: List[str]


def _build_refresh_context() -> _RefreshContext:
    from core.infrastructure.fx_provider import DolarAPIProvider
    from core.infrastructure.futures_provider import (
        RofexProvider, DEFAULT_SYMBOLS as ROFEX_SYMBOLS,
        parse_contract_maturity, implied_tna, resolve_spot_for_tna,
    )
    from core.infrastructure.indices_provider import BCRAIndicesProvider
    from core.infrastructure.rem_provider import REMProvider
    repo = ExcelInstrumentsRepository(MASTER_XLSX)
    provider = Data912MarketDataProvider()
    bond_panels = (
        ("bonares", SOBERANOS, {}),
        ("cer", CER, {"include_category": True}),
        ("bopreales", BOPREALES, {}),
        ("tasa_fija", TASA_FIJA, {"include_dias": True}),
        ("dolar_linked", DOLAR_LINKED, {}),
        ("tamar", TAMAR, {"include_dias": True}),
    )
    # Union de instrument_types — drives un único batched execute() por ciclo
    # en vez de uno por panel (cada execute() tiene overhead fijo).
    all_bond_types = list(dict.fromkeys(
        list(SOBERANOS) + list(CER) + list(BOPREALES) + list(TASA_FIJA)
        + list(DOLAR_LINKED) + list(TAMAR) + list(DUAL_TAMAR)
    ))
    return _RefreshContext(
        repo=repo,
        provider=provider,
        use_case=GenerateMonitorReport(repo, provider),
        fx=DolarAPIProvider(),
        rofex=RofexProvider(),
        bcra=BCRAIndicesProvider(),
        rem=REMProvider(),
        rofex_symbols=ROFEX_SYMBOLS,
        parse_contract_maturity=parse_contract_maturity,
        implied_tna=implied_tna,
        resolve_spot_for_tna=resolve_spot_for_tna,
        bond_panels=bond_panels,
        all_bond_types=all_bond_types,
    )


def _log_master_vs_supported_diff(repo) -> None:
    """Boot-time diagnostic: tickers en data912 historical no dados de alta en master."""
    try:
        local_tickers = {inst.ticker.upper() for inst in repo.get_all_instruments() if inst.ticker}
        missing = sorted(HISTORICAL_SUPPORTED_TICKERS - local_tickers)
        if missing:
            logger.warning(
                "data912 historical soporta %d tickers no dados de alta en master: %s",
                len(missing), ", ".join(missing),
            )
        else:
            logger.info("data912 historical: todos los tickers soportados están en master.")
    except (AttributeError, TypeError) as e:
        logger.debug(f"Diff master↔data912 historical falló: {e}")


def _prefetch_panel_lider(provider) -> None:
    """Carga inicial paralela del OHLC histórico (evita blow-up del 1er ciclo)."""
    from concurrent.futures import ThreadPoolExecutor
    def _one(t):
        try:
            provider.fetch_stock_history(t)
        except Exception as e:
            logger.warning(f"Prefetch history {t} failed: {e}")
    logger.info(f"Prefetching {len(PANEL_LIDER)} Panel Líder OHLC histories...")
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_one, PANEL_LIDER))
    logger.info(f"Prefetch complete in {(time.monotonic()-t0)*1000:.0f}ms")


def _refresh_fx_strip(ctx: _RefreshContext, snapshot: Snapshot) -> None:
    """FX dolarapi + TAMAR TNA + Reservas BCRA + Riesgo País → chip strip del header."""
    try:
        fx_data = dict(ctx.fx.get_all())
        tamar_tna = ctx.bcra.get_tamar()
        if tamar_tna is not None:
            fx_data["tamar"] = {
                "nombre": "TAMAR (TNA)", "compra": None, "venta": tamar_tna,
                "fechaActualizacion": None,
            }
        reservas = ctx.bcra.get_reservas_brutas()
        delta    = ctx.bcra.get_reservas_delta()
        if reservas is not None:
            fx_data["_bcra_macro"] = {"reservas": reservas, "delta": delta}
        try:
            from core.infrastructure.argentinadatos_provider import get_provider as _ard
            rp = _ard().get_riesgo_pais()
            if rp:
                fx_data["_riesgo_pais"] = rp
        except Exception:
            logger.debug("Riesgo País fetch skipped", exc_info=True)
        snapshot.update_fx(fx_data)
    except Exception:
        logger.exception("FX/TAMAR refresh failed")


def _fetch_bond_metrics(ctx: _RefreshContext, snapshot: Snapshot) -> dict:
    """Single batched execute() → metrics agrupados por instrument_type.
    Marca todos los panels como error si falla."""
    try:
        all_metrics = ctx.use_case.execute(ctx.all_bond_types)
        metrics_by_type: dict = {}
        for m in all_metrics:
            t = m.snapshot.instrument.instrument_type
            metrics_by_type.setdefault(t, []).append(m)
        return metrics_by_type
    except Exception as e:
        logger.exception("Bond metrics batch failed")
        for mid, _, _ in ctx.bond_panels:
            snapshot.update_monitor(mid, status="error",
                                    subtitle=f"Error en refresh: {type(e).__name__}")
        return {}


def _fit_log_curve(pairs: List[tuple]) -> Optional[Tuple[float, float]]:
    """Fit TIR = a + b·ln(DM) over (DM, TIR) pairs. Returns (a, b) or None."""
    if len(pairs) < 3:
        return None
    try:
        ln_dms = np.array([math.log(p[0]) for p in pairs])
        tirs   = np.array([p[1]           for p in pairs])
        b, a   = np.polyfit(ln_dms, tirs, 1)
        return (a, b)
    except Exception:
        return None


def _augment_bond_calc_result(result: dict, snapshot: "Snapshot",
                              rem_provider=None) -> None:
    """Agrega TIR real, Spread vs curva y Carry+Roll al resultado de bond_calculate.

    Requiere el snapshot actual para obtener la curva del panel tasa_fija
    (log-fit TIR vs DM). Si el panel no tiene suficientes datos, las métricas
    de curva quedan en None pero TIR real se computa igual (sólo necesita REM).
    Los valores se devuelven como fracciones decimales (igual que el resto del
    bundle de métricas de bond_detail).
    """
    tir = result.get("tir")
    if tir is None:
        return

    # TIR real vs inflación esperada REM-BCRA
    if rem_provider is not None:
        try:
            yoy = rem_provider.get_next_12m_yoy()
            if yoy is not None and yoy > -1.0:
                result["tir_real"] = (1.0 + tir) / (1.0 + yoy) - 1.0
        except Exception:
            pass

    # Spread vs curva y Carry+Roll: requieren la curva log del panel
    dm = result.get("duration")
    if not dm or dm <= 0:
        return
    snap_data = snapshot.get() if snapshot is not None else {}
    tf_monitor = next(
        (m for m in snap_data.get("monitors", []) if m.get("id") == "tasa_fija"),
        None,
    )
    if not tf_monitor:
        return
    rows = tf_monitor.get("rows", [])
    valid = [
        (r["duration"], r["tir"] / 100.0)
        for r in rows
        if r.get("duration") and r["duration"] > 0 and r.get("tir") is not None
    ]
    fit = _fit_log_curve(valid)
    if fit is None:
        return
    try:
        a, b = fit
        tir_fitted = a + b * math.log(dm)
        result["spread_curva"] = tir - tir_fitted
        # Carry+Roll 30d: TEM + roll_down
        tem = result.get("tem")  # decimal fraction (e.g. 0.0178)
        dm_rolled = dm - _ONE_MONTH_YEARS
        if tem is not None and dm_rolled > 0.001:
            tir_rolled = a + b * math.log(dm_rolled)
            roll_down = -dm * (tir_rolled - tir)
            result["carry_roll"] = tem + roll_down
    except Exception:
        pass


_MES_ES_MAP = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
               "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12}


def _parse_mes_label(label: Optional[str]) -> Optional[tuple]:
    """Etiqueta de mes del sendero BEI ('may-26') → (2026, 5). None si no parsea."""
    try:
        mmm, yy = str(label).lower().strip().split("-")
        return (2000 + int(yy), _MES_ES_MAP[mmm])
    except (ValueError, KeyError, AttributeError):
        return None


def _enrich_curve_metrics(rows: list, *, rem_annual: Optional[float] = None,
                          compute_tir_real: bool = False) -> None:
    """Añade Spread vs curva (rich/cheap), Carry+Roll 30d y —opcional— TIR real
    a las filas de un panel de bonos, en lugar. Genérico: sirve para cualquier
    curva (CER, soberanos, DL, TAMAR, tasa fija).

    Cross-sectional: ajusta TIR = a + b·ln(DM) sobre TODAS las filas del panel y
    mide el residuo de cada bono contra ese ajuste. Trabaja puramente sobre los
    dicts de fila (funciona también para filas sintéticas DUAL_TF/_TAM que no
    tienen InstrumentMetrics disponible).

    Fórmulas:
      Spread c.= TIR − TIR_ajuste_logcurva(DM)  → positivo = sobre-rinde = barato
      C+R 30d  = TEM + roll_down_30d  donde roll_down = −MD×Δy de curva
      TIR real = (1+TIR)/(1+REM_12m) − 1  (solo compute_tir_real: paneles en
                 pesos nominales. Para CER la TIR ya es real; para hard-dollar
                 el REM en pesos no aplica.)
    """
    # ---- Ajuste log: TIR = a + b·ln(DM) sobre todos los bonos del panel ----
    valid = [
        (r["duration"], r["tir"] / 100.0)
        for r in rows
        if r.get("duration") and r["duration"] > 0 and r.get("tir") is not None
    ]
    log_fit = _fit_log_curve(valid)  # (a, b) tal que tir_fit = a + b*ln(dm)

    for row in rows:
        dm    = row.get("duration")
        tir_p = row.get("tir")      # % units, e.g. 21.25
        tem_p = row.get("tem")      # % units, e.g. 1.60
        tir = tir_p / 100.0 if tir_p is not None else None
        tem = tem_p / 100.0 if tem_p is not None else None

        # TIR real vs REM interanual (solo paneles en pesos nominales)
        if compute_tir_real and tir is not None and rem_annual is not None and rem_annual > -1.0:
            row["tir_real"] = _scale_pct((1.0 + tir) / (1.0 + rem_annual) - 1.0)

        if log_fit is None or not dm or dm <= 0 or tir is None:
            continue

        a, b = log_fit
        tir_fitted = a + b * math.log(dm)

        # Carry + Roll 30d: TEM + roll_down (roll_down ≈ −MD × Δy de la curva)
        dm_rolled = dm - _ONE_MONTH_YEARS
        if tem is not None and dm_rolled > 0.001:
            tir_rolled = a + b * math.log(dm_rolled)
            roll_down  = -dm * (tir_rolled - tir)   # positivo si curva sube
            row["carry_roll"] = _scale_pct(tem + roll_down)

        # Spread vs curva (pp): positivo = bono barato, negativo = caro
        row["spread_curva"] = _scale_pct(tir - tir_fitted)


def _enrich_tasa_fija_rows(rows: list, rem_annual: Optional[float]) -> None:
    """tasa_fija: métricas curve-relative + TIR real (vía _enrich_curve_metrics)
    MÁS DV01 y Convexidad. La convexidad usa la aproximación zero-coupon
    (C = t(t+1)/(1+y)²), exacta para LECAP/BONCAP capitalizables."""
    _enrich_curve_metrics(rows, rem_annual=rem_annual, compute_tir_real=True)
    for row in rows:
        dm    = row.get("duration")
        price = row.get("price")
        dias  = row.get("dias")
        tir_p = row.get("tir")
        tir = tir_p / 100.0 if tir_p is not None else None
        # DV01 = MD × Precio × _DV01_BP  (Fabozzi cap. 4, per 100 VN)
        if dm and price:
            row["dv01"] = round(dm * price * _DV01_BP, 4)
        # Convexidad (approx. zero-coupon bullet: C = t·(t+1)/(1+y)²)
        if dias and tir is not None:
            t = dias / 365.0
            row["convexity"] = round(t * (t + 1.0) / (1.0 + tir) ** 2, 2)


def _refresh_bond_panels(ctx: _RefreshContext, snapshot: Snapshot, today: date) -> None:
    """Standard bond panels. Filtra MEP-only para bonares/bopreales,
    re-valúa DUALES como TAMAR PURO en el panel TAMAR y como tasa fija en TASA FIJA."""
    metrics_by_type = _fetch_bond_metrics(ctx, snapshot)
    if not metrics_by_type:
        return

    def _select(types):
        out = []
        for t in types:
            out.extend(metrics_by_type.get(t, []))
        return out

    for mid, types, kwargs in ctx.bond_panels:
        try:
            metrics = _by_md_asc(_select(types))
            # Bonares/BOPREALES: solo MEP (sufijo D). Pesos/CABLE existen en
            # master para el popup histórico pero no en panel — ver agents.md.
            if mid in ("bonares", "bopreales"):
                metrics = [m for m in metrics
                           if m.snapshot.instrument and m.snapshot.instrument.ticker
                           and m.snapshot.instrument.ticker.upper().endswith("D")]
            rows = [_base_bond_row(m, today=today, **kwargs) for m in metrics]
            if mid == "tamar":
                rows.extend(_revalue_duales_as_tamar_puro(_select(DUAL_TAMAR), ctx.bcra, today, kwargs))
                rows.sort(key=lambda r: (r.get("duration") is None, r.get("duration") or 0.0))
            if mid == "tasa_fija":
                rows.extend(_revalue_duales_as_tasa_fija(_select(DUAL_TAMAR), today, kwargs))
                rows.sort(key=lambda r: (r.get("duration") is None, r.get("duration") or 0.0))

            # Enriquecimiento rich-cheap (Spread vs curva + Carry/Roll + TIR real).
            # Generalizado a todos los paneles de bonos: cada uno ajusta su propia
            # curva log TIR vs DM. tasa_fija/tamar (pesos nominales) suman TIR real
            # vs REM; CER (TIR ya real) y hard-dollar (REM en pesos no aplica) no.
            try:
                if mid == "tasa_fija":
                    _enrich_tasa_fija_rows(rows, rem_annual=ctx.rem.get_next_12m_yoy())
                elif mid == "tamar":
                    _enrich_curve_metrics(rows, rem_annual=ctx.rem.get_next_12m_yoy(),
                                          compute_tir_real=True)
                elif mid in ("cer", "bonares", "bopreales", "dolar_linked"):
                    _enrich_curve_metrics(rows)
            except Exception:
                logger.debug("%s rich-cheap enrichment failed", mid, exc_info=True)

            snapshot.update_monitor(mid, rows=rows, status="ok")
        except Exception as e:
            logger.exception(f"Monitor '{mid}' refresh failed")
            snapshot.update_monitor(mid, status="error",
                                    subtitle=f"Error en refresh: {type(e).__name__}")


# Etiqueta de grupo por panel para el ranking transversal de Valor Relativo.
_RV_GROUP_LABEL = {
    "bonares": "Soberano", "bopreales": "Bopreal", "cer": "CER",
    "tasa_fija": "Tasa Fija", "tamar": "TAMAR", "dolar_linked": "DL",
}


def _refresh_valor_relativo(snapshot: Snapshot, top_n: int = 12) -> None:
    """Ranking rich/cheap transversal. Reúne el `spread_curva` que cada panel de
    bonos ya calculó este ciclo y muestra los más baratos (spread +, sobre-rinden
    vs su curva) arriba y los más caros (spread −) abajo. No recalcula nada: lee
    del snapshot recién actualizado por _refresh_bond_panels."""
    try:
        snap = snapshot.get()
        pool = []
        for m in snap.get("monitors", []):
            grupo = _RV_GROUP_LABEL.get(m.get("id"))
            if not grupo:
                continue
            for r in (m.get("rows") or []):
                sc = r.get("spread_curva")
                if sc is None:
                    continue
                pool.append({
                    "ticker": r.get("ticker"),
                    "grupo": grupo,
                    "duration": r.get("duration"),
                    "tir": r.get("tir"),
                    "spread_curva": sc,
                    "carry_roll": r.get("carry_roll"),
                })
        pool.sort(key=lambda r: r["spread_curva"], reverse=True)
        # Más baratos (top) + más caros (bottom); sin solapar si hay pocos puntos.
        rows = pool[:top_n] + pool[-top_n:] if len(pool) > 2 * top_n else pool
        snapshot.update_monitor("valor_relativo", rows=rows, status="ok")
    except Exception:
        logger.exception("valor_relativo refresh failed")
        snapshot.update_monitor("valor_relativo", status="error",
                                subtitle="Error en refresh")


def _revalue_duales_as_tamar_puro(dual_metrics, bcra, today, kwargs):
    """DUAL bonds re-valuados ignorando el floor fijo (como si fueran TAMAR puro).
    Sufijo '_TAM' para distinguir del DUAL original."""
    from core.domain.services import FinancialEngine
    from dataclasses import replace
    out = []
    for dm in dual_metrics:
        if not dm.snapshot.price:
            continue
        tir, vtec, md = FinancialEngine.recompute_as_tamar_puro(dm.snapshot, indices_provider=bcra)
        if tir is None:
            continue
        new_m = replace(dm, tir=tir, technical_value=vtec, duration=md)
        if new_m.technical_value and new_m.snapshot.price:
            new_m.parity = new_m.snapshot.price / new_m.technical_value
        row = _base_bond_row(new_m, today=today, **kwargs)
        row["ticker"] = f"{dm.snapshot.instrument.ticker}_TAM"
        out.append(row)
    return out


def _revalue_duales_as_tasa_fija(dual_metrics, today, kwargs):
    """DUAL bonds re-valuados como tasa fija pura — TAMAR forzado a cero para
    que el floor siempre gane el max(). Sufijo '_TF'. Solo DUAL con floor."""
    from core.domain.services import FinancialEngine
    from dataclasses import replace
    out = []
    for dm in dual_metrics:
        if not dm.snapshot.price:
            continue
        tir, vtec, md = FinancialEngine.recompute_as_tasa_fija(dm.snapshot, indices_provider=None)
        if tir is None:
            continue
        new_m = replace(dm, tir=tir, technical_value=vtec, duration=md)
        if new_m.technical_value and new_m.snapshot.price:
            new_m.parity = new_m.snapshot.price / new_m.technical_value
        row = _base_bond_row(new_m, today=today, **kwargs)
        row["ticker"] = f"{dm.snapshot.instrument.ticker}_TF"
        out.append(row)
    return out


def _refresh_futuros(ctx: _RefreshContext, snapshot: Snapshot) -> None:
    """Rofex DLR curve: TNA implícita = (futuro_last/spot)^(365/d) - 1."""
    try:
        quotes = ctx.rofex.get_quotes(ctx.rofex_symbols)
        # Spot para TNA implícita: política híbrida horario. Durante rueda
        # BYMA (lun-vie 11-17 ARG) → mid mayorista (live, intraday). Fuera
        # de rueda → A3500 oficial BCRA (cierre EOD). Fallback recíproco si
        # alguna fuente está caída. Ver `resolve_spot_for_tna` para detalles.
        spot = ctx.resolve_spot_for_tna(ctx.fx, ctx.bcra)
        rows_fut = []
        for sym in ctx.rofex_symbols:
            q = quotes.get(sym)
            if not q:
                continue
            mat = ctx.parse_contract_maturity(sym)
            last = q.get("last")
            tna = ctx.implied_tna(last, spot, mat) if (last and spot and mat) else None
            rows_fut.append({
                "ticker": sym, "vto": mat,
                "bid": q.get("bid"), "ask": q.get("ask"),
                "last": last, "settle": q.get("settle"),
                "tna": _scale_pct(tna),
                "open_interest": q.get("open_interest"), "volume": q.get("volume"),
            })
        snapshot.update_monitor("futuros", rows=rows_fut, status="ok")
    except Exception as e:
        logger.exception("Monitor 'futuros' refresh failed")
        snapshot.update_monitor("futuros", status="error",
                                subtitle=f"Error en refresh: {type(e).__name__}")


def _refresh_panel_lider(ctx: _RefreshContext, snapshot: Snapshot) -> None:
    """Acciones BYMA: mid + 5d/30d change + sparkline 30-trading-day."""
    try:
        stock_snaps = ctx.provider.fetch_snapshots(PANEL_LIDER)
        rows_stocks = []
        for t in PANEL_LIDER:
            s = stock_snaps.get(t)
            if s is None:
                continue
            mid = (s.bid + s.ask) / 2.0 if (s.bid and s.ask) else None
            hist = ctx.provider.fetch_stock_history(t)
            close_5d = _close_n_trading_days_ago(hist, 5) if hist else None
            close_30d = _close_at_calendar_days_ago(hist, 30) if hist else None
            spark = [float(r["c"]) for r in (hist[-30:] if hist else []) if r.get("c") is not None]
            rows_stocks.append({
                "ticker": t,
                "bid": s.bid, "ask": s.ask, "mid": mid,
                "change_pct": s.change_pct,
                "change_5d": _pct_change(mid, close_5d),
                "change_30d": _pct_change(mid, close_30d),
                "sparkline": spark,
                "volume": s.volume, "operations": s.operations,
            })
        snapshot.update_monitor("panel_lider", rows=rows_stocks, status="ok")
    except Exception as e:
        logger.exception("Monitor 'panel_lider' refresh failed")
        snapshot.update_monitor("panel_lider", status="error",
                                subtitle=f"Error en refresh: {type(e).__name__}")


class _CycleStats:
    """Stats accumulator: heartbeat per-cycle a DEBUG + summary INFO cada 60s."""
    SUMMARY_EVERY_S = 60.0

    def __init__(self):
        self.last_summary_ts = time.monotonic()
        self.count = 0
        self.overruns = 0
        self.max_ms = 0.0
        self.sum_ms = 0.0

    def observe(self, elapsed_s: float, sleep_for_s: float) -> None:
        self.count += 1
        ms = elapsed_s * 1000
        self.sum_ms += ms
        if ms > self.max_ms:
            self.max_ms = ms
        logger.debug(
            "Heartbeat OK at %s — cycle=%.0fms, next in %.0fms",
            datetime.now().strftime("%H:%M:%S"), ms, sleep_for_s * 1000,
        )
        if elapsed_s > REFRESH_SEC * 2:
            logger.warning(
                "Cycle overran budget hard: %.2fs > %ds (>2x) — skipping sleep",
                elapsed_s, REFRESH_SEC,
            )
            self.overruns += 1
        elif elapsed_s > REFRESH_SEC:
            logger.debug(
                "Cycle %.2fs > budget %ds (within 2x) — skipping sleep",
                elapsed_s, REFRESH_SEC,
            )
            self.overruns += 1

    def maybe_log_summary(self) -> None:
        now = time.monotonic()
        if (now - self.last_summary_ts) < self.SUMMARY_EVERY_S or self.count == 0:
            return
        logger.info(
            "Refresh summary: %d cycles in %.0fs · avg=%.0fms · max=%.0fms · overruns=%d",
            self.count, now - self.last_summary_ts,
            self.sum_ms / self.count, self.max_ms, self.overruns,
        )
        self.last_summary_ts = now
        self.count = 0
        self.overruns = 0
        self.max_ms = 0.0
        self.sum_ms = 0.0


# FX dolarapi cambia lento intraday → invalidar cada 5s era overkill.
# Mantener invalidación periódica cada 30s y dejar que el TTL=60s del
# provider absorba los ciclos intermedios.
_FX_INVALIDATE_EVERY_S = 30


def _refresh_loop(snapshot: Snapshot):
    """Orquestador: init dependencias + loop {invalidate → fetch all panels →
    adaptive sleep}. Cada panel encapsulado en su propia función para
    aislamiento y testabilidad."""
    ctx = _build_refresh_context()
    _log_master_vs_supported_diff(ctx.repo)
    _prefetch_panel_lider(ctx.provider)

    stats = _CycleStats()
    last_fx_invalidate = 0.0

    while not _SHUTDOWN_EVENT.is_set():
        loop_start = time.monotonic()
        today = date.today()

        # Invalidación de caches ANTES del fetch (sino TTL podría expirar
        # mid-cycle y forzar segunda round-trip).
        ctx.provider.invalidate_cache()
        if (loop_start - last_fx_invalidate) >= _FX_INVALIDATE_EVERY_S:
            ctx.fx.invalidate_cache()
            last_fx_invalidate = loop_start

        _refresh_fx_strip(ctx, snapshot)
        _refresh_bond_panels(ctx, snapshot, today)
        _refresh_valor_relativo(snapshot)
        _refresh_futuros(ctx, snapshot)
        _refresh_panel_lider(ctx, snapshot)

        elapsed = time.monotonic() - loop_start
        sleep_for = max(0.0, REFRESH_SEC - elapsed)
        stats.observe(elapsed, sleep_for)
        stats.maybe_log_summary()
        if _SHUTDOWN_EVENT.wait(sleep_for):
            break
    logger.info("Refresh loop: shutdown signal received, exiting.")


def _bei_refresh_loop(snapshot: Snapshot):
    """Dedicated thread for BEI extendido — heavier than the main loop because
    of bootstrap + 4 NSS fits + REM fetch + Rofex. Runs once eagerly at startup,
    then every BEI_REFRESH_SEC. Decoupled so bond panels keep updating fast.
    """
    repo = ExcelInstrumentsRepository(MASTER_XLSX)
    provider = Data912MarketDataProvider()
    use_case = GenerateMonitorReport(repo, provider)
    from core.infrastructure.indices_provider import BCRAIndicesProvider
    bcra = BCRAIndicesProvider()
    from apps.cli.monitors.bei import compute_bei_tables

    while not _SHUTDOWN_EVENT.is_set():
        try:
            logger.info("BEI thread: computing extended tables...")
            tables = compute_bei_tables(use_case=use_case, indices_provider=bcra)
            if tables is None:
                logger.warning("BEI thread: compute returned None (insufficient data)")
                snapshot.update_monitor("bei_tenor", status="error",
                                        subtitle="Datos de mercado insuficientes para ajustar curvas")
                snapshot.update_monitor("bei_sendero", status="error")
                snapshot.update_monitor("bei_pares", status="error")
            else:
                meta = tables["meta"]
                g = meta.get("gamma")
                rem_y = meta.get("rem_12m_yoy")
                subtitle = f"{meta['fits']}"
                if g:
                    subtitle += f" · γ={g:.4f}"
                if rem_y is not None:
                    subtitle += f" · REM 12m i.a.={rem_y*100:.2f}%"

                tenor_rows = [{
                    "plazo": r["plazo"], "dias": r["dias"],
                    "tea_nominal": _scale_pct(r["tea_nominal"]),
                    "tea_real": _scale_pct(r["tea_real"]),
                    "tamar_fwd": _scale_pct(r["tamar_fwd"]),
                    "bei_spot": _scale_pct(r["bei_spot"]),
                    "bei_fwd": _scale_pct(r["bei_fwd"]),
                    "bei_g_adj": _scale_pct(r["bei_g_adj"]),
                    "bei_tamar": _scale_pct(r["bei_tamar"]),
                    "dev_implicita": _scale_pct(r["dev_implicita"]),
                    "tc_real": _scale_pct(r["tc_real"]),
                } for r in tables["tenor"]]
                sendero_rows = [{
                    "mes": r["mes"], "dias_mes": r["dias_mes"],
                    "bei_mensual": _scale_pct(r["bei_mensual"]),
                    "rem_mensual": _scale_pct(r["rem_mensual"]),
                    "rem_projected": r.get("rem_projected", False),
                    "diff": _scale_pct(r["diff"]),
                } for r in tables["sendero"]]
                pair_rows = [{
                    "lecap": r["lecap"], "boncer": r["boncer"],
                    "vto_lecap": r["vto_lecap"], "vto_cer": r["vto_cer"],
                    "dias": r["dias"],
                    "delta_m1": _scale_pct(r["delta_m1"]),
                    "infl_mensual_impl": _scale_pct(r["infl_mensual_impl"]),
                } for r in tables["pares"]]

                snapshot.update_monitor("bei_tenor", rows=tenor_rows,
                                        subtitle=subtitle, status="ok")
                snapshot.update_monitor("bei_sendero", rows=sendero_rows, status="ok")
                snapshot.update_monitor("bei_pares", rows=pair_rows, status="ok")
                logger.info(
                    "BEI thread: ok — %d tenor rows, %d sendero rows, %d pair rows",
                    len(tenor_rows), len(sendero_rows), len(pair_rows),
                )
        except Exception as e:
            # Shutdown race: durante Ctrl+C los daemon threads pueden quedar
            # mid-cycle y ThreadPoolExecutor tira RuntimeError. Salimos limpios.
            if _is_shutdown_error(e):
                logger.info("BEI thread: shutdown detected, exiting.")
                return
            logger.exception("BEI thread: refresh failed")
            snapshot.update_monitor("bei_tenor", status="error",
                                    subtitle="Error en cálculo BEI — ver logs")
            snapshot.update_monitor("bei_sendero", status="error")
            snapshot.update_monitor("bei_pares", status="error")
        if _SHUTDOWN_EVENT.wait(BEI_REFRESH_SEC):
            break
    logger.info("BEI thread: shutdown signal received, exiting.")

# --------------------------------------------------------------------------- #
# HTTP Handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    snapshot = None
    # Singletons compartidos por endpoint /api/bond_detail y /api/bond_calculate.
    # Reusar las instancias del refresh loop aprovecha sus caches class-level
    # (BCRA, FX) — sino cada click re-fetchearía CER + TAMAR + FX.
    bond_repo = None
    bond_provider = None
    bond_indices = None   # BCRAIndicesProvider — también usado por /api/bcra_data
    bond_fx = None
    cafci = None          # CAFCIProvider — FCI catalog + daily returns (/api/fci)
    def log_message(self, fmt, *args): pass

    # El cliente (browser SPA) cierra TCP mid-response cuando navega o
    # cambia de pestaña; eso emerge como ConnectionAbortedError/Reset/Broken
    # adentro de socketserver y socketserver lo printea como traceback feo
    # al stderr. Acá lo atajamos y lo bajamos a DEBUG — no es bug del server,
    # es comportamiento normal del lado client.
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            logger.debug(f"Client disconnected mid-response: {type(e).__name__}")
            self.close_connection = True
    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/snapshot":
            payload = json.dumps(self.snapshot.get(), default=_json_default).encode("utf-8")
            return self._send(HTTPStatus.OK, payload, "application/json; charset=utf-8")
        if path == "/api/bei_history":
            return self._serve_bei_history()
        if path == "/api/supported_tickers":
            # Single source of truth: el frontend consume esta lista en vez
            # de tener una copia hardcoded en app.js (que terminaba en drift).
            body = json.dumps({
                "tickers": sorted(HISTORICAL_SUPPORTED_TICKERS),
            }).encode("utf-8")
            return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        if path.startswith("/api/bond_history/"):
            ticker = path[len("/api/bond_history/"):]
            return self._serve_bond_history(ticker)
        if path.startswith("/api/stock_history/"):
            ticker = path[len("/api/stock_history/"):]
            return self._serve_stock_history(ticker)
        if path.startswith("/api/bond_detail/"):
            ticker = path[len("/api/bond_detail/"):]
            qs = parse_qs(urlparse(self.path).query or "")
            lag = _safe_lag(qs.get("lag", ["1"])[0])
            tf_raw = qs.get("tamar_forecast", [None])[0]
            try:
                tf = float(tf_raw) if tf_raw not in (None, "") else None
            except (TypeError, ValueError):
                tf = None
            return self._serve_bond_detail(ticker, lag, tamar_forecast=tf)
        if path == "/api/abm/schemas":
            return self._send(
                HTTPStatus.OK,
                json.dumps({"schemas": instruments_abm.SHEET_SCHEMAS}).encode("utf-8"),
                "application/json; charset=utf-8",
            )
        if path == "/api/abm/instruments":
            try:
                items = instruments_abm.list_instruments(MASTER_XLSX)
                body = json.dumps({"items": items}).encode("utf-8")
                return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
            except Exception as e:
                logger.exception("ABM list failed")
                return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                                  json.dumps({"error": str(e)}).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if path.startswith("/api/abm/instrument/"):
            ticker = path[len("/api/abm/instrument/"):]
            try:
                inst = instruments_abm.get_instrument(MASTER_XLSX, ticker)
                if inst is None:
                    return self._send(HTTPStatus.NOT_FOUND,
                                      json.dumps({"error": "not_found", "ticker": ticker}).encode("utf-8"),
                                      "application/json; charset=utf-8")
                return self._send(HTTPStatus.OK, json.dumps(inst).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                logger.exception(f"ABM get {ticker} failed")
                return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                                  json.dumps({"error": str(e)}).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if path.startswith("/api/letras/prefill/"):
            return self._serve_letras_prefill(path[len("/api/letras/prefill/"):])
        if path == "/api/rem_bei_path":
            return self._serve_rem_bei_path()
        if path == "/api/all_cashflows":
            return self._serve_all_cashflows()
        if path == "/api/bcra_data":
            return self._serve_bcra_data()
        if path == "/api/cartera":
            return self._serve_cartera()
        if path == "/api/fci":
            return self._serve_fci()
        if path.startswith("/api/fci/"):
            return self._serve_fci_detail(path[len("/api/fci/"):])
        if path in ("/", "/index.html"): return self._serve_static("index.html")
        if path in ("/cashflows", "/cashflows.html"): return self._serve_static("cashflows.html")
        if path in ("/bcra", "/bcra.html"): return self._serve_static("bcra.html")
        if path in ("/cartera", "/cartera.html"): return self._serve_static("cartera.html")
        if path.startswith("/static/"): return self._serve_static(path[len("/static/"):])
        self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            return self._send(HTTPStatus.BAD_REQUEST,
                              b'{"error":"invalid JSON"}',
                              "application/json; charset=utf-8")

        if path == "/api/abm/preview_cashflows":
            # Genera cashflows synth desde los fields del form SIN persistir.
            # Frontend lo usa para preview/regenerar.
            if not _preview_rate_allow():
                return self._send(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    json.dumps({"error": "rate limit (120 req/min)"}).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
            try:
                fields = payload.get("fields") or {}
                cfs = instruments_abm._synth_cashflows_for_fields(fields)
                return self._send(HTTPStatus.OK,
                                  json.dumps({"cashflows": cfs}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except (ValueError, TypeError, KeyError) as e:
                logger.warning(f"preview_cashflows invalid input: {e}")
                return self._send(HTTPStatus.OK,
                                  json.dumps({"cashflows": [], "error": str(e)}).encode("utf-8"),
                                  "application/json; charset=utf-8")

        if path.startswith("/api/bond_calculate/"):
            ticker = path[len("/api/bond_calculate/"):]
            return self._serve_bond_calculate(ticker, payload)

        if path.startswith("/api/cer_scenarios/"):
            ticker = path[len("/api/cer_scenarios/"):]
            return self._serve_cer_scenarios(ticker, payload)

        if path == "/api/cartera":
            return self._serve_cartera_save(payload)

        if path == "/api/scenario":
            return self._serve_scenario(payload)

        if path == "/api/abm/instrument":
            try:
                sheet = payload.get("sheet")
                fields = payload.get("fields") or {}
                # cashflows opcional: si viene, se persisten en hoja Cashflows
                # (reemplazando lo existente). Si no, se preserva el synth.
                cashflows = payload.get("cashflows")
                result = instruments_abm.save_instrument(
                    MASTER_XLSX, sheet, fields, cashflows=cashflows,
                )
                return self._send(HTTPStatus.OK, json.dumps(result).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except ValueError as e:
                return self._send(HTTPStatus.BAD_REQUEST,
                                  json.dumps({"error": str(e)}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                logger.exception("ABM save failed")
                return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                                  json.dumps({"error": str(e)}).encode("utf-8"),
                                  "application/json; charset=utf-8")
        return self._send(HTTPStatus.NOT_FOUND, b'{"error":"not found"}',
                          "application/json; charset=utf-8")

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/cartera/"):
            return self._serve_cartera_delete(path[len("/api/cartera/"):])
        if path.startswith("/api/abm/instrument/"):
            ticker = path[len("/api/abm/instrument/"):]
            try:
                result = instruments_abm.delete_instrument(MASTER_XLSX, ticker)
                status = HTTPStatus.OK if result["action"] == "deleted" else HTTPStatus.NOT_FOUND
                return self._send(status, json.dumps(result).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                logger.exception(f"ABM delete {ticker} failed")
                return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                                  json.dumps({"error": str(e)}).encode("utf-8"),
                                  "application/json; charset=utf-8")
        return self._send(HTTPStatus.NOT_FOUND, b'{"error":"not found"}',
                          "application/json; charset=utf-8")

    def _serve_bei_history(self):
        """Return the BEI daily history CSV as JSON for charting.

        Caches the serialized JSON keyed by CSV mtime — re-reads only when
        the file actually changes (typically once/day when the BEI thread
        appends). Avoids parsing+to_dict on every poll.
        """
        from config.settings import DATA_DIR as _DD
        csv_path = os.path.join(_DD, "history", "bei_diario.csv")
        if not os.path.isfile(csv_path):
            return self._send(HTTPStatus.OK, b'{"rows":[]}', "application/json; charset=utf-8")
        try:
            mtime = os.path.getmtime(csv_path)
            with _BEI_HISTORY_LOCK:
                cached = _BEI_HISTORY_CACHE.get(csv_path)
                hit = cached and cached[0] == mtime
                body = cached[1] if hit else None
            if not hit:
                # I/O y parsing FUERA del lock — sino bloqueamos otros endpoints.
                df = pd.read_csv(csv_path)
                rows = df.where(pd.notna(df), None).to_dict(orient="records")
                body = json.dumps({"rows": rows}, default=_json_default).encode("utf-8")
                with _BEI_HISTORY_LOCK:
                    _BEI_HISTORY_CACHE[csv_path] = (mtime, body)
            return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            logger.debug("BEI history: client disconnected mid-send")
        except Exception as e:
            logger.exception(f"BEI history serve failed: {e}")
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b'{"error":"history unreadable"}',
                              "application/json; charset=utf-8")
    def _serve_data912_history(
        self, ticker: str,
        ticker_re: re.Pattern,
        supported: frozenset,
        cache: dict,
        lock: threading.Lock,
        ttl_s: float,
        base_url: str,
        source: str,
    ) -> None:
        """Proxy genérico para /historical/{bonds,stocks}/<ticker> de data912.
        Rate limit upstream 120/min: el TTL cache absorbe clicks rápidos.
        Evicta entradas vencidas al escribir para acotar uso de memoria."""
        from core.infrastructure._http import http_get_json
        if not ticker_re.match(ticker):
            return self._send(HTTPStatus.BAD_REQUEST,
                              json.dumps({"error": "invalid ticker"}).encode("utf-8"),
                              "application/json; charset=utf-8")
        t = ticker.upper()
        if t not in supported:
            return self._send(HTTPStatus.BAD_REQUEST,
                              json.dumps({"error": "ticker not supported", "ticker": t}).encode("utf-8"),
                              "application/json; charset=utf-8")
        now = time.time()
        with lock:
            cached = cache.get(t)
            if cached and (now - cached[0]) < ttl_s:
                return self._send(HTTPStatus.OK,
                                  json.dumps({"ticker": t, "points": cached[1]}).encode("utf-8"),
                                  "application/json; charset=utf-8")
        try:
            data = http_get_json(f"{base_url}/{t}", timeout=10, source=source)
            if not isinstance(data, list):
                raise ValueError(f"unexpected response: {type(data).__name__}")
        except Exception as e:
            logger.warning(f"{source} fetch {t} failed: {e}")
            return self._send(HTTPStatus.BAD_GATEWAY,
                              json.dumps({"error": str(e), "ticker": t}).encode("utf-8"),
                              "application/json; charset=utf-8")
        with lock:
            expired = [k for k, (ts, _) in cache.items() if now - ts > ttl_s]
            for k in expired:
                del cache[k]
            cache[t] = (now, data)
        return self._send(HTTPStatus.OK,
                          json.dumps({"ticker": t, "points": data}).encode("utf-8"),
                          "application/json; charset=utf-8")

    def _serve_bond_history(self, ticker: str):
        return self._serve_data912_history(
            ticker, _BOND_HISTORY_TICKER_RE, HISTORICAL_SUPPORTED_TICKERS,
            _BOND_HISTORY_CACHE, _BOND_HISTORY_LOCK, _BOND_HISTORY_TTL_S,
            _BOND_HISTORY_BASE, "Data912/bond_hist",
        )

    def _serve_stock_history(self, ticker: str):
        return self._serve_data912_history(
            ticker, _STOCK_HISTORY_TICKER_RE, STOCK_HISTORY_SUPPORTED_TICKERS,
            _STOCK_HISTORY_CACHE, _STOCK_HISTORY_LOCK, _STOCK_HISTORY_TTL_S,
            _STOCK_HISTORY_BASE, "Data912/stock_hist",
        )

    def _serve_bond_detail(self, ticker: str, settlement_lag: int = 1,
                           tamar_forecast: Optional[float] = None):
        """Detalle estático del bono: meta + cashflows futuros + métricas vivas."""
        if not _BOND_DETAIL_TICKER_RE.match(ticker):
            return self._send(HTTPStatus.BAD_REQUEST,
                              json.dumps({"error": "invalid ticker"}).encode("utf-8"),
                              "application/json; charset=utf-8")
        try:
            detail = bond_detail.get_bond_detail(
                ticker, self.bond_repo, self.bond_provider,
                self.bond_indices, self.bond_fx,
                historical_supported=HISTORICAL_SUPPORTED_TICKERS,
                settlement_lag=settlement_lag,
                tamar_forecast=tamar_forecast,
            )
            if detail is None:
                return self._send(HTTPStatus.NOT_FOUND,
                                  json.dumps({"error": "not_found", "ticker": ticker}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            body = json.dumps(detail, default=_json_default).encode("utf-8")
            return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        except Exception as e:
            logger.exception(f"bond_detail {ticker} failed")
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                              json.dumps({"error": str(e)}).encode("utf-8"),
                              "application/json; charset=utf-8")

    def _serve_bond_calculate(self, ticker: str, payload: dict):
        """Recalcula métricas dado un precio (clean|dirty) o un TIR objetivo.
        Rate-limited igual que /api/abm/preview_cashflows (mismo bucket) — el
        endpoint puede ser polleado por keystroke del usuario."""
        if not _BOND_DETAIL_TICKER_RE.match(ticker):
            return self._send(HTTPStatus.BAD_REQUEST,
                              json.dumps({"error": "invalid ticker"}).encode("utf-8"),
                              "application/json; charset=utf-8")
        if not _preview_rate_allow():
            return self._send(HTTPStatus.TOO_MANY_REQUESTS,
                              json.dumps({"error": "rate limit (120 req/min)"}).encode("utf-8"),
                              "application/json; charset=utf-8")
        try:
            mode = payload.get("mode")
            if mode not in ("from_price", "from_tir"):
                return self._send(HTTPStatus.BAD_REQUEST,
                                  json.dumps({"error": "mode must be from_price|from_tir"}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            price = payload.get("price")
            price_mode = payload.get("price_mode", "dirty")
            tir = payload.get("tir")
            try:
                price = float(price) if price is not None else None
                tir = float(tir) if tir is not None else None
            except (TypeError, ValueError):
                return self._send(HTTPStatus.BAD_REQUEST,
                                  json.dumps({"error": "invalid number for price/tir"}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            lag = _safe_lag(payload.get("settlement_lag", 1))
            tf_raw = payload.get("tamar_forecast")
            try:
                tf = float(tf_raw) if tf_raw not in (None, "") else None
            except (TypeError, ValueError):
                tf = None
            result = bond_detail.calculate(
                ticker, self.bond_repo, self.bond_provider,
                self.bond_indices, self.bond_fx,
                mode=mode, price=price, price_mode=price_mode, tir=tir,
                settlement_lag=lag, tamar_forecast=tf,
            )
            if result is None:
                return self._send(HTTPStatus.NOT_FOUND,
                                  json.dumps({"error": "not_found_or_invalid", "ticker": ticker}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            # Enrich with TIR real (REM), Spread vs curva y Carry+Roll del panel
            _augment_bond_calc_result(result, self.snapshot,
                                      rem_provider=getattr(self, "rem_provider", None))
            body = json.dumps(result, default=_json_default).encode("utf-8")
            return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        except Exception as e:
            logger.exception(f"bond_calculate {ticker} failed")
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                              json.dumps({"error": str(e)}).encode("utf-8"),
                              "application/json; charset=utf-8")

    def _serve_cer_scenarios(self, ticker: str, payload: dict):
        """Retorno nominal por escenario de inflación (REM / BEI mercado / editable)
        para un bono CER, partiendo del precio dirty actual. Mismo rate-limit que
        bond_calculate (puede pollearse por keystroke al editar 'Mi escenario')."""
        if not _BOND_DETAIL_TICKER_RE.match(ticker):
            return self._send(HTTPStatus.BAD_REQUEST,
                              json.dumps({"error": "invalid ticker"}).encode("utf-8"),
                              "application/json; charset=utf-8")
        if not _preview_rate_allow():
            return self._send(HTTPStatus.TOO_MANY_REQUESTS,
                              json.dumps({"error": "rate limit (120 req/min)"}).encode("utf-8"),
                              "application/json; charset=utf-8")
        try:
            price = payload.get("price_dirty")
            try:
                price = float(price) if price is not None else None
            except (TypeError, ValueError):
                price = None
            lag = _safe_lag(payload.get("settlement_lag", 1))
            ci_raw = payload.get("custom_infl_monthly")
            try:
                custom = float(ci_raw) if ci_raw not in (None, "") else None
            except (TypeError, ValueError):
                custom = None
            # Mapa custom por mes (modo Custom): {"YYYY-MM": tasa_decimal} → {(y,m): r}.
            cm_raw = payload.get("custom_monthly")
            custom_monthly = {}
            if isinstance(cm_raw, dict):
                for k, v in cm_raw.items():
                    try:
                        y, m = str(k).split("-")
                        custom_monthly[(int(y), int(m))] = float(v)
                    except (ValueError, TypeError):
                        continue
            # BEI mensual de mercado desde el sendero ya computado (percent → decimal).
            snap = self.__class__.snapshot.get()
            sendero = next(
                (m["rows"] for m in snap.get("monitors", []) if m["id"] == "bei_sendero"),
                [],
            )
            bei_monthly = {}
            for r in sendero:
                ym = _parse_mes_label(r.get("mes"))
                bm = r.get("bei_mensual")
                if ym is not None and bm is not None:
                    bei_monthly[ym] = bm / 100.0
            result = bond_detail.cer_return_scenarios(
                ticker, self.bond_repo, self.bond_provider,
                self.bond_indices, self.bond_fx,
                price_dirty=price, settlement_lag=lag,
                custom_infl_monthly=custom,
                custom_monthly=custom_monthly or None,
                rem_provider=getattr(self, "rem_provider", None),
                bei_monthly=bei_monthly,
            )
            if result is None:
                return self._send(HTTPStatus.NOT_FOUND,
                                  json.dumps({"error": "not_found", "ticker": ticker}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            body = json.dumps(result, default=_json_default).encode("utf-8")
            return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        except Exception as e:
            logger.exception(f"cer_scenarios {ticker} failed")
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                              json.dumps({"error": str(e)}).encode("utf-8"),
                              "application/json; charset=utf-8")

    def _serve_letras_prefill(self, ticker: str):
        """Datos de una letra desde ArgentinaDatos para pre-llenar el form ABM."""
        from core.infrastructure.argentinadatos_provider import get_provider as _ard
        if not re.match(r"^[A-Za-z0-9]{2,12}$", ticker):
            return self._send(HTTPStatus.BAD_REQUEST,
                              json.dumps({"error": "invalid ticker"}).encode("utf-8"),
                              "application/json; charset=utf-8")
        row = _ard().get_by_ticker(ticker)
        if row is None:
            return self._send(HTTPStatus.NOT_FOUND,
                              json.dumps({"error": "not_found", "ticker": ticker}).encode("utf-8"),
                              "application/json; charset=utf-8")
        body = json.dumps({
            "ticker":        str(row.get("ticker", "")).upper().strip(),
            "fecha_emision": str(row.get("fechaEmision") or "")[:10],
            "fecha_pago":    str(row.get("fechaVencimiento") or "")[:10],
            "tem_licit":     row.get("tem"),
            "vpv":           row.get("vpv"),
        }).encode("utf-8")
        return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")

    def _serve_all_cashflows(self):
        """Todos los cashflows futuros de todos los instrumentos en el master."""
        try:
            repo = self.__class__.bond_repo
            today = date.today()
            result = []
            for inst in repo.get_all_instruments():
                future_cfs = inst.get_future_cashflows(today)
                if not future_cfs:
                    continue
                ticker = (inst.ticker or "").upper()
                is_usd = inst.instrument_type in ("BONAR", "GLOBAL", "BOPREAL") and ticker.endswith("D")
                result.append({
                    "ticker": inst.ticker,
                    "short_name": inst.short_name,
                    "type": inst.instrument_type,
                    "currency": "USD" if is_usd else "ARS",
                    "cashflows": [
                        {"date": cf.date.isoformat(),
                         "amortization": _safe_num(cf.amortization),
                         "interest": _safe_num(cf.interest),
                         "total": _safe_num(cf.total)}
                        for cf in future_cfs
                    ],
                })
            body = json.dumps({"instruments": result, "as_of": today.isoformat()},
                              default=_json_default).encode("utf-8")
            return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        except Exception as e:
            logger.exception("all_cashflows failed")
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                              json.dumps({"error": str(e)}).encode("utf-8"),
                              "application/json; charset=utf-8")

    def _mep_usd_ars(self) -> Optional[float]:
        """TC para convertir posiciones USD (MEP) a ARS: MEP → CCL → mayorista (venta)."""
        fx = self.__class__.bond_fx
        for casa in ("mep", "ccl", "mayorista"):
            try:
                q = fx.get_quote(casa)
            except Exception:
                q = None
            if q and q.get("venta"):
                return float(q["venta"])
        return None

    def _cartera_metrics_by_ticker(self) -> dict:
        """{TICKER: {price,tir,md,grupo,spread_curva,currency}} desde el snapshot
        vivo de los paneles de bonos. El primer panel que contiene el ticker gana
        (evita duplicar el mismo MEP en dos paneles)."""
        snap = self.__class__.snapshot.get()
        out: dict = {}
        for m in snap.get("monitors", []):
            grupo = _RV_GROUP_LABEL.get(m.get("id"))
            if not grupo:
                continue
            usd_panel = m["id"] in ("bonares", "bopreales")
            for r in (m.get("rows") or []):
                tk = (r.get("ticker") or "").upper()
                if not tk or tk in out:
                    continue
                out[tk] = {
                    "price": r.get("price"),
                    "tir": r.get("tir"),
                    "md": r.get("duration"),
                    "convexity": r.get("convexity"),
                    "grupo": grupo,
                    "spread_curva": r.get("spread_curva"),
                    "currency": "USD" if (usd_panel and tk.endswith("D")) else "ARS",
                }
        return out

    def _serve_cartera(self):
        """Valuación viva de la cartera: posiciones + KPIs + calendario de flujos."""
        try:
            holdings = cartera_store.list_holdings()
            metrics = self._cartera_metrics_by_ticker()
            result = portfolio_engine.build_portfolio(
                holdings, metrics, fx_usd_ars=self._mep_usd_ars(),
            )
            repo = self.__class__.bond_repo
            insts = {(i.ticker or "").upper(): i for i in repo.get_all_instruments()}
            for p in result["positions"]:
                inst = insts.get(p["ticker"])
                if inst and not p.get("short_name"):
                    p["short_name"] = inst.short_name
            result["cashflows"] = portfolio_engine.portfolio_cashflows(holdings, insts)
            result["holdings"] = holdings
            body = json.dumps(result, default=_json_default).encode("utf-8")
            return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        except Exception as e:
            logger.exception("cartera serve failed")
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                              json.dumps({"error": str(e)}).encode("utf-8"),
                              "application/json; charset=utf-8")

    def _serve_cartera_save(self, payload: dict):
        try:
            res = cartera_store.upsert_holding(
                payload.get("ticker"), payload.get("nominal"),
                cost_price=payload.get("cost_price"), note=payload.get("note", ""),
            )
            return self._send(HTTPStatus.OK, json.dumps(res).encode("utf-8"),
                              "application/json; charset=utf-8")
        except ValueError as e:
            return self._send(HTTPStatus.BAD_REQUEST,
                              json.dumps({"error": str(e)}).encode("utf-8"),
                              "application/json; charset=utf-8")
        except Exception as e:
            logger.exception("cartera save failed")
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                              json.dumps({"error": str(e)}).encode("utf-8"),
                              "application/json; charset=utf-8")

    def _serve_cartera_delete(self, ticker: str):
        try:
            res = cartera_store.delete_holding(ticker)
            status = HTTPStatus.OK if res["action"] == "deleted" else HTTPStatus.NOT_FOUND
            return self._send(status, json.dumps(res).encode("utf-8"),
                              "application/json; charset=utf-8")
        except Exception as e:
            logger.exception("cartera delete failed")
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                              json.dumps({"error": str(e)}).encode("utf-8"),
                              "application/json; charset=utf-8")

    def _serve_scenario(self, payload: dict):
        """Stress test: ΔP por bono + P&L de cartera ante (Δtir bps, ΔFX %).
        Rate-limited (mismo bucket que preview/calculate) — el panel postea por slider."""
        if not _preview_rate_allow():
            return self._send(HTTPStatus.TOO_MANY_REQUESTS,
                              json.dumps({"error": "rate limit (120 req/min)"}).encode("utf-8"),
                              "application/json; charset=utf-8")

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        try:
            d_tir = _f(payload.get("d_tir_bps"))
            d_fx = _f(payload.get("d_fx_pct"))
            snap = self.__class__.snapshot.get()
            bonds = []
            for m in snap.get("monitors", []):
                grupo = _RV_GROUP_LABEL.get(m.get("id"))
                if not grupo:
                    continue
                usd_panel = m["id"] in ("bonares", "bopreales")
                for r in (m.get("rows") or []):
                    tk = (r.get("ticker") or "").upper()
                    md = r.get("duration")
                    if not tk or md is None:
                        continue
                    ccy = "USD" if (usd_panel and tk.endswith("D")) else "ARS"
                    sh = scenario_engine.shock_position(
                        price=r.get("price"), md=md, convexity=r.get("convexity"),
                        currency=ccy, grupo=grupo, d_tir_bps=d_tir, d_fx_pct=d_fx,
                    )
                    bonds.append({
                        "ticker": tk, "grupo": grupo, "currency": ccy,
                        "price": r.get("price"), "md": md,
                        "dp_pct": _scale_pct(sh["dp_pct"]),
                        "dp_ars_pct": _scale_pct(sh["dp_ars_pct"]),
                        "new_price": sh["new_price"],
                    })
            bonds.sort(key=lambda b: (b["dp_ars_pct"] is None, b["dp_ars_pct"] or 0.0))

            portfolio = None
            holdings = cartera_store.list_holdings()
            if holdings:
                metrics = self._cartera_metrics_by_ticker()
                built = portfolio_engine.build_portfolio(
                    holdings, metrics, fx_usd_ars=self._mep_usd_ars())
                for p in built["positions"]:
                    p["convexity"] = metrics.get(p["ticker"], {}).get("convexity")
                ps = scenario_engine.portfolio_shock(
                    built["positions"], d_tir_bps=d_tir, d_fx_pct=d_fx)
                portfolio = {
                    "pnl_ars": ps["pnl_ars"],
                    "pnl_pct": _scale_pct(ps["pnl_pct"]),
                    "base_value_ars": ps["base_value_ars"],
                }
            body = json.dumps({
                "scenario": {"d_tir_bps": d_tir, "d_fx_pct": d_fx},
                "bonds": bonds, "portfolio": portfolio,
            }, default=_json_default).encode("utf-8")
            return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        except Exception as e:
            logger.exception("scenario failed")
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                              json.dumps({"error": str(e)}).encode("utf-8"),
                              "application/json; charset=utf-8")

    def _serve_rem_bei_path(self):
        """Sendero mensual BEI vs REM-BCRA para el chart de letras (LECAP/BONCAP).
        Lee del estado ya computado por el hilo BEI — sin cómputo propio."""
        try:
            snap = self.__class__.snapshot.get()
            sendero = next(
                (m["rows"] for m in snap.get("monitors", []) if m["id"] == "bei_sendero"),
                []
            )
            body = json.dumps({"rows": sendero}, default=_json_default).encode("utf-8")
            return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        except Exception as e:
            logger.exception("rem_bei_path failed")
            return self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                json.dumps({"error": str(e)}).encode("utf-8"),
                "application/json; charset=utf-8",
            )

    def _serve_bcra_data(self):
        """Datos BCRA para la página Monitor BCRA: catálogo + KPIs + historiales."""
        try:
            bcra = self.__class__.bond_indices
            today = date.today()
            reservas   = bcra.get_reservas_brutas()
            delta      = bcra.get_reservas_delta()
            tamar      = bcra.get_tamar()
            a3500      = bcra.get_a3500()
            catalog    = bcra.get_catalog()
            res_hist   = bcra.get_reservas_history(days=90)
            a3500_hist = bcra.get_a3500_history(days=90)
            tamar_hist = bcra.get_tamar_history(days=90)
            body = json.dumps({
                "as_of": today.isoformat(),
                "kpis": {
                    "reservas":       reservas,
                    "reservas_delta": delta,
                    "tamar_tna":      tamar,
                    "a3500":          a3500,
                },
                "catalog": catalog,
                "history": {
                    "reservas": res_hist,
                    "a3500":    a3500_hist,
                    "tamar":    tamar_hist,
                },
            }, default=_json_default).encode("utf-8")
            return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        except Exception as e:
            logger.exception("bcra_data failed")
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                              json.dumps({"error": str(e)}).encode("utf-8"),
                              "application/json; charset=utf-8")

    def _serve_fci(self):
        """FCI (CAFCI) catalog + daily returns, filtrable y buscable.

        Datos diarios (no live) → endpoint propio, fuera del snapshot de 5s.
        Query params: tipo, moneda, q (búsqueda), sort (período), dir (asc|desc),
        metric (tna|directo), limit."""
        try:
            cafci = self.__class__.cafci
            qs = parse_qs(urlparse(self.path).query or "")

            def _q(name, default=None):
                v = qs.get(name, [default])[0]
                return v if v not in ("", None) else default

            try:
                limit = int(_q("limit")) if _q("limit") is not None else None
            except (TypeError, ValueError):
                limit = None
            funds = cafci.list_funds(
                tipo_renta=_q("tipo"),
                moneda=_q("moneda"),
                query=_q("q"),
                sort=_q("sort", "mes_1"),
                direction=_q("dir", "desc"),
                metric=_q("metric", "tna"),
                limit=limit,
            )
            body = json.dumps({"meta": cafci.get_meta(), "funds": funds},
                              default=_json_default).encode("utf-8")
            return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            logger.debug("fci: client disconnected mid-send")
        except Exception as e:
            logger.exception("fci serve failed")
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                              json.dumps({"error": str(e)}).encode("utf-8"),
                              "application/json; charset=utf-8")

    def _serve_fci_detail(self, clase_id: str):
        """Una clase de FCI por clase_id (para el popup de detalle)."""
        try:
            fund = self.__class__.cafci.get_fund(clase_id)
            if fund is None:
                return self._send(HTTPStatus.NOT_FOUND,
                                  json.dumps({"error": "not_found", "clase_id": clase_id}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            body = json.dumps(fund, default=_json_default).encode("utf-8")
            return self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        except Exception as e:
            logger.exception(f"fci detail {clase_id} failed")
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                              json.dumps({"error": str(e)}).encode("utf-8"),
                              "application/json; charset=utf-8")

    def _serve_static(self, rel):
        rel = rel.replace("\\", "/").lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
        ext = os.path.splitext(full)[1].lower()
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".png": "image/png"}.get(ext, "application/octet-stream")
        with open(full, "rb") as f: self._send(HTTPStatus.OK, f.read(), ctype)

def main():
    logger.info("Starting Web Server...")
    snapshot = Snapshot()
    Handler.snapshot = snapshot

    # Limpiar instrumentos vencidos ANTES de cargar el repo, de modo que
    # el repositorio ya arranca con datos actualizados.
    from apps.web.instruments_abm import purge_matured_instruments
    _purged = purge_matured_instruments(MASTER_XLSX)
    if _purged:
        logger.info(
            "Startup cleanup: %d instrumento(s) vencido(s) eliminado(s): %s",
            len(_purged),
            [p["ticker"] for p in _purged],
        )
    else:
        logger.info("Startup cleanup: sin instrumentos vencidos.")

    # Singletons para los endpoints /api/bond_detail y /api/bond_calculate.
    # Instanciados acá (no en cada request) para reusar caches class-level
    # (Data912 3s TTL, BCRA disk-mirror, FX 60s, etc.).
    from core.infrastructure.fx_provider import DolarAPIProvider
    from core.infrastructure.indices_provider import BCRAIndicesProvider
    Handler.bond_repo = ExcelInstrumentsRepository(MASTER_XLSX)
    Handler.bond_provider = Data912MarketDataProvider()
    Handler.bond_indices = BCRAIndicesProvider()
    Handler.bond_fx = DolarAPIProvider()
    from core.infrastructure.rem_provider import REMProvider
    Handler.rem_provider = REMProvider()
    from core.infrastructure.cafci_provider import CAFCIProvider
    Handler.cafci = CAFCIProvider()
    # Prime CAFCI en background: el primer /api/fci no paga el fetch de 3.9MB
    # (o hidrata rápido desde el mirror en disco si ya existe).
    threading.Thread(target=Handler.cafci._ensure_loaded, daemon=True).start()

    threading.Thread(target=_refresh_loop, args=(snapshot,), daemon=True).start()
    threading.Thread(target=_bei_refresh_loop, args=(snapshot,), daemon=True).start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    logger.info(f"Web Server running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user (Ctrl+C).")
    except Exception:
        logger.exception("Web server crashed with unhandled exception.")
    finally:
        # Avisar a los background threads ANTES de cerrar el socket — sino
        # quedan mid-cycle pegados a una operación pesada (compute_bei_tables,
        # ThreadPoolExecutor.submit) mientras el interpreter se desmonta y
        # tiran RuntimeError("cannot schedule new futures after shutdown").
        _SHUTDOWN_EVENT.set()
        logger.info("Closing server socket.")
        server.server_close()

if __name__ == "__main__":
    main()
