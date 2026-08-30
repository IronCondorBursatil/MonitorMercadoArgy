"""Futures market data — Matba/Rofex via WebSocket público de Primary.

Reemplaza la integración previa con pyRofex (que requería credenciales
remarkets/live, no publicaba DLR/SPOT en demo, y agregaba ~5MB de deps).

**Fuente**: `wss://matbarofex.primary.ventures/ws` — el mismo WebSocket
público que usa la web de Matba "A3 - Real Time" (modo `guest`, sin auth).

**Protocolo** (reverse-engineered del bundle main.js de Matba):

  - URL: `wss://matbarofex.primary.ventures/ws?session_id=&conn_id=`
  - Subscribe: `{"_req":"S","topicType":"md","topics":["md.<sec_id>", ...],
                "replace":true}`
  - Unsubscribe: `{"_req":"U","topicType":"md","topics":[...],"replace":false}`
  - Heartbeat: enviar string literal `"ping"` cada ~30s; server responde `"pong"`.
  - Server pushea snapshots iniciales (BATCH JSON array) y luego updates
    individuales con prefix `M:`.

**Mensaje M: (texto pipe-delimited)**:

  `M:<security_id>|<seq>|<bidSz>|<bid>|<ask>|<askSz>|<lastPx>|<lastTs>|
     <lastSz>|<dailyVol>|<dailyOps>|<dailyHigh>|<dailyLow>|<dailyOpen>|
     <openInterest>|<settle>|<settleDate>|<prevSettle>|<prevDate>|...`

  Campos vacíos cuando no hay dato (ej. sin trades del día → lastPx vacío).

**Spot DLR**: el spot que Matba publica como base de la curva DLR es el
índice BCRA Comunicación A3500 (security `rx_DDF_BCRA_A3500`, ticker
"Dólar USA" en la UI). Lo mantenemos expuesto como `DLR/SPOT` en la
interface pública para no romper consumers (server.py).

Thread model: un único thread daemon mantiene la conexión WS abierta de por
vida del proceso. Reconnect con backoff exponencial (1s → 60s cap). State
compartido vía dict + lock.
"""

import asyncio
import calendar
import json
import logging
import threading
from datetime import date
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_WS_URL = "wss://matbarofex.primary.ventures/ws?session_id=&conn_id="
_WS_ORIGIN = "https://matbarofex.primary.ventures"
_PING_INTERVAL_S = 30.0
_RECONNECT_MIN_S = 1.0
_RECONNECT_MAX_S = 60.0

# Mapping de meses abreviados ESP (como los usa Matba en los tickers) a num.
_MONTHS = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12,
}

# Universo de contratos DLR (full curve a 12 meses) + spot A3500. Los
# tickers públicos del proyecto siguen siendo "DLR/MAY26" estilo pyRofex;
# acá los mapeamos internamente al security_id que usa Primary.
DEFAULT_SYMBOLS: List[str] = [
    "DLR/MAY26", "DLR/JUN26", "DLR/JUL26", "DLR/AGO26", "DLR/SEP26",
    "DLR/OCT26", "DLR/NOV26", "DLR/DIC26",
    "DLR/ENE27", "DLR/FEB27", "DLR/MAR27", "DLR/ABR27",
]
SPOT_SYMBOL = "DLR/SPOT"

# Security IDs internos de Primary. `DLR/XXX` -> `rx_DDF_DLR_XXX`,
# y SPOT mapea al índice A3500 publicado por BCRA.
_SPOT_SECURITY_ID = "rx_DDF_BCRA_A3500"


def _ticker_to_security_id(ticker: str) -> Optional[str]:
    """`DLR/MAY26` -> `rx_DDF_DLR_MAY26`. SPOT mapea a `rx_DDF_BCRA_A3500`.
    Devuelve None si el formato no es reconocido."""
    if ticker == SPOT_SYMBOL:
        return _SPOT_SECURITY_ID
    if "/" not in ticker:
        return None
    base, suffix = ticker.split("/", 1)
    return f"rx_DDF_{base}_{suffix}"


def parse_contract_maturity(symbol: str) -> Optional[date]:
    """DLR/MAY26 -> 2026-05-31 (last calendar day of contract month).
    Mantengo la misma convención que el provider anterior (= último día
    del mes del contrato) para compatibilidad con el cálculo de TNA.
    """
    if "/" not in symbol:
        return None
    tail = symbol.split("/", 1)[1].rstrip("M").strip().upper()
    if len(tail) < 5:
        return None
    mon, yy = tail[:3], tail[3:5]
    if mon not in _MONTHS or not yy.isdigit():
        return None
    year = 2000 + int(yy)
    month = _MONTHS[mon]
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def implied_tea(future_price: float, spot: float, mat: date,
                today: Optional[date] = None) -> Optional[float]:
    """TEA implícita (efectiva anual compuesta) = (futuro/spot)^(365/days) - 1,
    decimal fraction. OJO: NO es la TNA (tasa nominal anual lineal) — para esa,
    usar (futuro/spot − 1)·365/d (ver `panels_rows._implied_rates`)."""
    if not (future_price and spot and spot > 0 and mat):
        return None
    today = today or date.today()
    days = (mat - today).days
    if days <= 0:
        return None
    try:
        return (future_price / spot) ** (365 / days) - 1
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


# --------------------------------------------------------------------------- #
# Spot resolver para TNA implícita (híbrido horario)
# --------------------------------------------------------------------------- #

# Horario de rueda BYMA (ARG = UTC-3, sin DST). Se computa con tz explícita de
# Buenos Aires para ser correcto independientemente de la timezone del server
# (cloud/otra región), no asumiendo AR local — consistente con holiday_engine.
_MARKET_OPEN_HOUR = 11
_MARKET_CLOSE_HOUR = 17


def _is_market_hours() -> bool:
    """True si estamos en horario de rueda BYMA (lun-vie, 11:00-17:00 ARG).
    No chequea feriados — durante feriados igual cae al fallback A3500 del
    último hábil, que es lo correcto."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires"))
    return now.weekday() < 5 and _MARKET_OPEN_HOUR <= now.hour < _MARKET_CLOSE_HOUR


def resolve_spot_for_tna(fx_provider, indices_provider) -> Optional[float]:
    """Spot DLR para calcular TNA implícita de futuros, política híbrida:

      1. **Durante rueda** (lun-vie 11-17 ARG): mid mayorista dolarapi —
         es lo más live e intraday.
      2. **Al cierre / fuera de rueda**: BCRA A3500 var 5 — fixing oficial
         publicado al cierre de cada día hábil, más estable para overnight.
      3. **Fallback** (BCRA no disponible o sin datos recientes): mayorista
         mid de dolarapi.

    Devuelve None sólo si AMBAS fuentes fallan. Loggea WARN si cae a fallback.
    """
    if _is_market_hours():
        mid = fx_provider.get_mayorista_mid() if fx_provider else None
        if mid:
            return mid
        # Si dolarapi falla durante rueda, intentar A3500 igual
        if indices_provider:
            a3500 = indices_provider.get_a3500()
            if a3500:
                logger.warning("spot TNA: dolarapi caído en rueda — fallback a A3500")
                return a3500
        return None
    # Fuera de rueda: A3500 primero
    if indices_provider:
        a3500 = indices_provider.get_a3500()
        if a3500:
            return a3500
    # Fallback final
    if fx_provider:
        mid = fx_provider.get_mayorista_mid()
        if mid:
            logger.warning("spot TNA: A3500 no disponible — fallback a mayorista mid")
            return mid
    return None


# --------------------------------------------------------------------------- #
# Parser de mensajes M: (pipe-delimited)
# --------------------------------------------------------------------------- #

def _safe_float(v: str) -> Optional[float]:
    """Empty string -> None. Sino float, o None si no parsea."""
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


# Posiciones (re-validadas contra el feed live 04/06/2026 — corrige el orden
# high/low y la semántica de [8..10], que el comentario original tenía mal):
#   [0]  "M:<security_id>"  (con prefijo M:)
#   [1]  seq number (interno de Primary, lo ignoramos)
#   [2]  bid size                    [3]  bid price
#   [4]  ask price                   [5]  ask size
#   [6]  last price                  (vacío si no hubo trades hoy)
#   [7]  last trade timestamp ISO    (idem)
#   [8]  daily volume (contratos operados hoy)
#   [9]  daily turnover (ARS, monto operado)   ← lo exponemos como "volume"
#   [10] daily volume (USD notional = contratos × 1000)
#   [11] daily LOW                   [12] daily HIGH         [13] daily open
#   [14] open interest
#   [15] settlement price            (ajuste vigente — vacío durante la rueda)
#   [16] settlement date (YYYY-MM-DD)
#   [17] prev settlement price       (→ habilita la Var 1D)
#   [18] prev settlement date
#   [19] prev2 settlement price      [20] prev2 settlement date
def _field(parts: List[str], i: int) -> Optional[float]:
    """parts[i] como float, o None si el índice no existe / no parsea."""
    return _safe_float(parts[i]) if i < len(parts) else None


def _parse_m_message(msg: str) -> Optional[tuple]:
    """Parsea un mensaje `M:` a (security_id, quote_dict). None si no parsea."""
    if not msg.startswith("M:"):
        return None
    parts = msg.split("|")
    if len(parts) < 16:
        return None
    security_id = parts[0][2:]  # strip "M:"
    quote = {
        "bid": _field(parts, 3),
        "ask": _field(parts, 4),
        "last": _field(parts, 6),
        "settle": _field(parts, 15),
        "volume": _field(parts, 9),              # monto operado en ARS
        "open_interest": _field(parts, 14),
        # Campos nuevos (popup de futuros): Var 1D, rango intradía, volumen en contratos.
        "prev_settle": _field(parts, 17),
        "day_open": _field(parts, 13),
        "day_high": _field(parts, 12),
        "day_low": _field(parts, 11),
        "volume_contracts": _field(parts, 8),
    }
    return security_id, quote


# --------------------------------------------------------------------------- #
# WebSocket client (thread daemon)
# --------------------------------------------------------------------------- #

class _MatbaWSClient:
    """Cliente WS persistente. Una instancia por proceso. Thread daemon que
    mantiene la conexión abierta y refleja el state en `quotes` (dict
    thread-safe). Auto-reconnect con backoff exponencial.
    """

    def __init__(self, security_ids: List[str]):
        self._security_ids = list(security_ids)
        self._topics = [f"md.{s}" for s in self._security_ids]
        self._quotes: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_forever, name="matba-ws", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get_quote(self, security_id: str) -> Optional[dict]:
        with self._lock:
            q = self._quotes.get(security_id)
            return dict(q) if q is not None else None

    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ------------------- internal ------------------- #

    def _run_forever(self) -> None:
        """Loop maestro: mantiene un asyncio loop dedicado al WS. El thread
        daemon vive por todo el proceso; reconnect lo hace el coroutine."""
        try:
            asyncio.run(self._async_main())
        except Exception:
            logger.exception("Matba WS thread crashed (won't auto-restart)")

    async def _async_main(self) -> None:
        backoff = _RECONNECT_MIN_S
        while not self._stop.is_set():
            try:
                await self._connect_and_run()
                backoff = _RECONNECT_MIN_S  # reset on clean disconnect
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"Matba WS error: {type(e).__name__}: {e}. "
                               f"Reconnect in {backoff:.0f}s")
                self._connected.clear()
            if self._stop.is_set():
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX_S)

    async def _connect_and_run(self) -> None:
        """Una sola conexión: subscribe → recv loop con heartbeat periódico."""
        try:
            import websockets
        except ImportError:
            logger.error("Matba WS: `websockets` no instalado "
                         "(pip install websockets). Panel futuros vacío.")
            self._stop.set()
            return

        async with websockets.connect(
            _WS_URL,
            additional_headers={
                "Origin": _WS_ORIGIN,
                "User-Agent": "Mozilla/5.0",
            },
            open_timeout=15,
            ping_interval=None,  # usamos heartbeat custom "ping" string
        ) as ws:
            self._connected.set()
            logger.info(f"Matba WS connected; subscribing {len(self._topics)} topics")
            sub_msg = json.dumps({
                "_req": "S",
                "topicType": "md",
                "topics": self._topics,
                "replace": True,
            })
            await ws.send(sub_msg)

            # Heartbeat task — mantiene la conexión viva (sin ping → server
            # cierra después de ~60-90s de inactividad).
            async def _heartbeat():
                while not self._stop.is_set():
                    await asyncio.sleep(_PING_INTERVAL_S)
                    try:
                        await ws.send("ping")
                    except Exception:
                        return

            hb = asyncio.create_task(_heartbeat())
            try:
                while not self._stop.is_set():
                    msg = await ws.recv()
                    self._handle_message(msg)
            finally:
                hb.cancel()
                self._connected.clear()

    def _handle_message(self, msg) -> None:
        """Tipos de mensajes:
        - "pong"               → respuesta heartbeat, ignorar
        - "X:{json}"           → eventos (clock, fixstatus, ...), ignorar
        - "M:..."              → market data tick (lo procesamos)
        - "[\"M:...\", ...]"   → batch JSON (snapshots iniciales y aglutinados)
        """
        if not isinstance(msg, str) or msg == "pong":
            return
        if msg.startswith("X:"):
            return
        if msg.startswith("["):
            # Batch: array JSON de strings M:...
            try:
                batch = json.loads(msg)
            except json.JSONDecodeError:
                return
            for item in batch:
                if isinstance(item, str):
                    self._process_m(item)
            return
        if msg.startswith("M:"):
            self._process_m(msg)

    def _process_m(self, msg: str) -> None:
        parsed = _parse_m_message(msg)
        if parsed is None:
            return
        security_id, quote = parsed
        with self._lock:
            # Merge: campos None del update no pisan valores previos (un tick
            # parcial puede traer sólo bid+ask sin tocar last/settle/etc).
            prev = self._quotes.get(security_id, {})
            for k, v in quote.items():
                if v is not None:
                    prev[k] = v
                elif k not in prev:
                    prev[k] = None
            self._quotes[security_id] = prev


# --------------------------------------------------------------------------- #
# Public provider — mismo shape de antes (`get_quotes(symbols) -> Dict`)
# --------------------------------------------------------------------------- #

class RofexProvider:
    """Provider de futuros DLR via WebSocket público de Matba/Primary.

    Drop-in replacement del provider anterior (pyRofex). Mismo método
    `get_quotes(symbols)` con la misma shape de retorno. No requiere
    credenciales — usa el modo `guest` de matbarofex.primary.ventures.

    El thread WS arranca lazy en el primer `get_quotes()` y persiste hasta
    el shutdown del proceso.
    """

    _lock = threading.Lock()
    _client: Optional[_MatbaWSClient] = None
    _warmup_logged: bool = False

    @classmethod
    def _ensure_client(cls, symbols: List[str]) -> _MatbaWSClient:
        with cls._lock:
            if cls._client is None:
                # Subscribirse a la unión de DEFAULT + cualquier extra pedido,
                # más siempre el spot A3500.
                all_tickers = set(DEFAULT_SYMBOLS) | set(symbols) | {SPOT_SYMBOL}
                sec_ids = [_ticker_to_security_id(t) for t in all_tickers]
                sec_ids = [s for s in sec_ids if s is not None]
                cls._client = _MatbaWSClient(sec_ids)
                cls._client.start()
            return cls._client

    def get_quotes(self, symbols: Optional[List[str]] = None) -> Dict[str, dict]:
        """Returns {ticker: {bid, ask, last, settle, volume, open_interest,
        prev_settle, day_open, day_high, day_low, volume_contracts}}.

        Para tickers sin datos todavía (WS frío en los primeros ~1-2s, o
        contrato sin actividad), el dict puede tener todos los valores None.
        Tickers desconocidos no aparecen en el dict de retorno.
        """
        if symbols is None:
            symbols = list(DEFAULT_SYMBOLS) + [SPOT_SYMBOL]
        client = self._ensure_client(symbols)

        # Warmup: el server tarda ~500ms-2s en enviar el primer batch. Si
        # nunca conectamos, no esperamos (devolvemos dict vacío).
        if not client.is_connected():
            client._connected.wait(timeout=2.5)

        out: Dict[str, dict] = {}
        for ticker in symbols:
            sec_id = _ticker_to_security_id(ticker)
            if sec_id is None:
                continue
            q = client.get_quote(sec_id)
            if q is None:
                continue
            out[ticker] = q
        if not RofexProvider._warmup_logged and out:
            logger.info(f"Matba WS warmup OK — {len(out)}/{len(symbols)} "
                        f"contracts received first quote")
            RofexProvider._warmup_logged = True
        return out
