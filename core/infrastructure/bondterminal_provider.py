"""BondTerminal provider — riesgo país calculado desde spreads de bonos AR.

Endpoint: GET https://bondterminal.com/api/riesgo-pais
Response: {weightedSpreadBps, ambitoValue, deltas, ambitoDeltas, bonds, ...}
TTL 5min — actualiza en tiempo real con cotizaciones IOL.

Dos valores de riesgo país:
  - valor_bps: spread ponderado BondTerminal (calculado desde los 6 bonos del EMBI AR)
  - ambito_bps: valor oficial Ambito/JP Morgan
"""

import logging
import threading
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_URL = "https://bondterminal.com/api/riesgo-pais"
_TTL_S = 300       # 5 minutos con dato bueno
_FAIL_TTL_S = 60   # backoff tras un fallo: no reintentar en CADA request


class BondTerminalProvider:
    def __init__(self):
        self._cache: Optional[dict] = None
        self._last_try: float = 0.0    # sello del último INTENTO (ok o fallido)
        self._last_ok: bool = False
        self._lock = threading.Lock()

    def get_riesgo_pais(self, *, force: bool = False) -> Optional[dict]:
        """Riesgo país de BondTerminal. Devuelve dict normalizado o None si no hay datos.

        Campos:
          valor_bps       spread ponderado calculado (float)
          ambito_bps      valor oficial Ambito (int)
          fecha           fecha ISO corta del snapshot (str)
          delta_1d/1w/1m  variación del spread BondTerminal (float)
          ambito_delta_1d/1w/1m  variación Ambito (int)
          bonds           lista de bonos incluidos en el cálculo
          sparkline       puntos históricos [{date, value}]
          data_quality    "live" | "delayed" | ...

        La llamada HTTP corre FUERA del lock: con el endpoint colgado, sostenerlo los
        10s del timeout serializaba N requests concurrentes en N×10s de threadpool. El
        lock sólo cubre el cache y el sello del intento, que se estampa SIEMPRE —
        también cuando el fetch falla— así que un provider caído se reintenta cada
        `_FAIL_TTL_S` y no una vez por request (negative caching)."""
        now = time.monotonic()
        with self._lock:
            ttl = _TTL_S if self._last_ok else _FAIL_TTL_S
            if not force and self._last_try and (now - self._last_try) < ttl:
                return self._cache
            self._last_try = now
        result = self._fetch()
        with self._lock:
            self._last_ok = result is not None
            if result is not None:
                self._cache = result
            return self._cache

    def _fetch(self) -> Optional[dict]:
        """Un GET normalizado, sin tocar el cache. None si falla o el payload no sirve."""
        try:
            resp = httpx.get(
                _URL,
                timeout=5.0,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            raw = resp.json()
        except Exception as e:
            logger.warning("BondTerminal riesgo país fetch failed: %s", e)
            return None
        if not isinstance(raw, dict) or "weightedSpreadBps" not in raw:
            return None
        deltas = raw.get("deltas") or {}
        ambito_deltas = raw.get("ambitoDeltas") or {}
        result = {
            "valor_bps": raw.get("weightedSpreadBps"),
            "ambito_bps": raw.get("ambitoValue"),
            "fecha": (raw.get("asOf") or "")[:10],
            "delta_1d": deltas.get("oneDay"),
            "delta_1w": deltas.get("oneWeek"),
            "delta_1m": deltas.get("oneMonth"),
            "ambito_delta_1d": ambito_deltas.get("oneDay"),
            "ambito_delta_1w": ambito_deltas.get("oneWeek"),
            "ambito_delta_1m": ambito_deltas.get("oneMonth"),
            "included_count": raw.get("includedCount"),
            "outstanding_millions": raw.get("totalOutstandingMillions"),
            "bonds": raw.get("bonds") or [],
            "sparkline": raw.get("sparklineData") or [],
            "data_quality": raw.get("dataQuality"),
            "metadata": raw.get("metadata") or {},
            "as_of": raw.get("asOf"),
        }
        logger.info(
            "BondTerminal riesgo país: %.1f bps (Ambito: %s) calidad=%s",
            result["valor_bps"] or 0, result["ambito_bps"], result["data_quality"])
        return result
