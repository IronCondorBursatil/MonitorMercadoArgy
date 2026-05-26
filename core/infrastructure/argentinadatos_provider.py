"""ArgentinaDatos provider — letras + riesgo país.

Letras endpoint: GET https://api.argentinadatos.com/v1/finanzas/letras
Response: [{ticker, fechaEmision, fechaVencimiento, tem, vpv}, ...]
TTL 1h — la Sec. Finanzas solo actualiza en días de licitación.

Riesgo país endpoint: GET https://api.argentinadatos.com/v1/finanzas/indices/riesgo-pais/ultimo
Response: {fecha, valor}  (valor en bps, e.g. 750)
TTL 5min — dato diario publicado por JP Morgan, actualiza 1-2x por día.

Histórico (para delta diario): GET https://api.argentinadatos.com/v1/finanzas/indices/riesgo-pais
TTL 1h — se usa solo para obtener el valor del día anterior.
"""

import logging
import threading
import time
from typing import List, Optional

from core.infrastructure._http import http_get_json

logger = logging.getLogger(__name__)

_LETRAS_URL = "https://api.argentinadatos.com/v1/finanzas/letras"
_TTL_S = 3600  # 1 hora

_RIESGO_PAIS_URL      = "https://api.argentinadatos.com/v1/finanzas/indices/riesgo-pais/ultimo"
_RIESGO_PAIS_HIST_URL = "https://api.argentinadatos.com/v1/finanzas/indices/riesgo-pais"
_RP_TTL_S      = 300   # 5 min — valor actual
_RP_HIST_TTL_S = 3600  # 1 hora — valor anterior (delta diario)


class ArgentinaDatosProvider:
    def __init__(self):
        self._cache: Optional[List[dict]] = None
        self._cache_ts: float = 0.0
        self._lock = threading.Lock()
        self._rp_cache: Optional[dict] = None
        self._rp_cache_ts: float = 0.0
        self._rp_lock = threading.Lock()
        self._rp_prev_valor: Optional[int] = None
        self._rp_prev_ts: float = 0.0

    def fetch_letras(self, *, force: bool = False) -> List[dict]:
        """Devuelve la lista completa de letras activas. Thread-safe."""
        with self._lock:
            if (
                not force
                and self._cache is not None
                and (time.monotonic() - self._cache_ts) < _TTL_S
            ):
                return self._cache
            try:
                data = http_get_json(
                    _LETRAS_URL,
                    timeout=10,
                    user_agent="balanz-monitor/1.0",
                    source="ArgentinaDatos/letras",
                )
                if not isinstance(data, list):
                    raise ValueError(f"expected list, got {type(data).__name__}")
                self._cache = data
                self._cache_ts = time.monotonic()
                logger.info("ArgentinaDatos: %d letras cargadas.", len(data))
            except Exception as e:
                logger.warning("ArgentinaDatos letras fetch failed: %s", e)
            return self._cache or []

    def get_by_ticker(self, ticker: str) -> Optional[dict]:
        """Busca una letra por ticker (case-insensitive). None si no existe."""
        t = ticker.upper().strip()
        for row in self.fetch_letras():
            if str(row.get("ticker", "")).upper().strip() == t:
                return row
        return None

    def _get_prev_valor(self) -> Optional[int]:
        """Valor del día anterior desde el histórico. Cache 1h."""
        if (
            self._rp_prev_valor is not None
            and (time.monotonic() - self._rp_prev_ts) < _RP_HIST_TTL_S
        ):
            return self._rp_prev_valor
        try:
            data = http_get_json(
                _RIESGO_PAIS_HIST_URL,
                timeout=15,
                user_agent="balanz-monitor/1.0",
                source="ArgentinaDatos/riesgo-pais-hist",
            )
            if isinstance(data, list) and len(data) >= 2:
                self._rp_prev_valor = data[-2]["valor"]
                self._rp_prev_ts = time.monotonic()
        except Exception as e:
            logger.warning("Riesgo País hist fetch failed: %s", e)
        return self._rp_prev_valor

    def get_riesgo_pais(self) -> Optional[dict]:
        """Último valor de riesgo país con variación diaria. Devuelve {valor, fecha, delta_abs, delta_pct} o None."""
        with self._rp_lock:
            if (
                self._rp_cache is not None
                and (time.monotonic() - self._rp_cache_ts) < _RP_TTL_S
            ):
                return self._rp_cache
            try:
                data = http_get_json(
                    _RIESGO_PAIS_URL,
                    timeout=10,
                    user_agent="balanz-monitor/1.0",
                    source="ArgentinaDatos/riesgo-pais",
                )
                if isinstance(data, dict) and "valor" in data:
                    valor = data["valor"]
                    fecha = data.get("fecha", "")
                    prev  = self._get_prev_valor()
                    delta_abs = (valor - prev) if prev is not None else None
                    delta_pct = round(delta_abs / prev * 100, 1) if prev else None
                    self._rp_cache = {
                        "valor": valor,
                        "fecha": fecha,
                        "delta_abs": delta_abs,
                        "delta_pct": delta_pct,
                    }
                    self._rp_cache_ts = time.monotonic()
                    logger.info(
                        "Riesgo País: %s bps (%s) Δ%+d (%+.1f%%)",
                        valor, fecha,
                        delta_abs if delta_abs is not None else 0,
                        delta_pct if delta_pct is not None else 0,
                    )
            except Exception as e:
                logger.warning("Riesgo País fetch failed: %s", e)
            return self._rp_cache


# Singleton a nivel de módulo — compartido por todos los endpoints.
_provider: Optional[ArgentinaDatosProvider] = None
_provider_lock = threading.Lock()


def get_provider() -> ArgentinaDatosProvider:
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                _provider = ArgentinaDatosProvider()
    return _provider
