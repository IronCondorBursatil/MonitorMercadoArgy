"""REM (Relevamiento de Expectativas de Mercado) del BCRA.

Fuente primaria: API pública mantenida por facundo allia
(https://github.com/facundoallia/rem-bcra-api), que re-publica el dataset
que el BCRA distribuye en PDF mensual.

Fallback: ArgentinaDatos /v1/finanzas/rem/ultimo — misma fuente (BCRA),
distinto wrapper. Se activa automáticamente si la primaria falla.

Schema interno (cache_rows):
  { "período": "2026-04-30" | "próx. 12 meses" | 2026,
    "referencia": "var. % mensual" | "var. % i.a." | ...,
    "mediana": 2.6, "promedio": 2.63 }

Cache: 6 horas en memoria. El REM se actualiza una vez por mes.
"""

import logging
import threading
import time
from datetime import date, datetime
from typing import Dict, List, Optional

from core.infrastructure._http import http_get_json

logger = logging.getLogger(__name__)

_BASE         = "https://bcra-rem-api.facujallia.workers.dev/api"
_ARD_REM_URL  = "https://argentinadatos.com/v1/finanzas/rem/ultimo"


def _parse_period(raw) -> Optional[date]:
    """REM 'período' field can be a date string, an int (year), or a label.
    Returns a date when interpretable; None for aggregate labels like
    'próx. 12 meses'. Strict matching: each format is tried against the
    full string, never a prefix slice, to avoid mis-parsing labels."""
    if isinstance(raw, int):
        return date(raw, 12, 31)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or "próx" in s.lower() or "prox" in s.lower():
        return None
    candidates = [s, s.split(" ")[0]] if " " in s else [s]
    for cand in candidates:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m"):
            try:
                return datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
    return None


class REMProvider:
    """Wraps the REM API with a thread-safe long-TTL cache y fallback a ArgentinaDatos."""

    URL_IPC    = f"{_BASE}/ipc_general"
    TTL_SECONDS = 6 * 3600  # REM updates monthly; long TTL avoids the 1 req/min limit.

    _lock          = threading.Lock()
    _cache_rows:   List[dict] = []
    _last_fetch_ts: float     = 0.0

    @staticmethod
    def _normalize_ard(rows: list) -> List[dict]:
        """Normaliza la respuesta de ArgentinaDatos al schema interno.

        ARD usa 'periodo' (sin tilde), devuelve array directo (sin wrapper 'datos').
        Mapeamos al schema que usan get_monthly_path() / get_next_12m_yoy().
        """
        out = []
        for row in rows:
            out.append({
                "período":   row.get("periodo", ""),   # tilde: compatible con _parse_period
                "referencia": row.get("referencia", ""),
                "mediana":   row.get("mediana"),
                "promedio":  row.get("promedio"),
            })
        return out

    def _fetch(self) -> None:
        with self._lock:
            if self._cache_rows and (time.time() - self._last_fetch_ts) < self.TTL_SECONDS:
                return

            rows: List[dict] = []

            # Primaria: facujallia.workers.dev
            try:
                payload = http_get_json(
                    self.URL_IPC, timeout=10, user_agent="Mozilla/5.0", source="REM/IPC",
                )
                rows = payload.get("datos") or []
            except Exception as e:
                logger.warning("REM primaria falló: %s — intentando ArgentinaDatos", e)

            # Fallback: ArgentinaDatos /v1/finanzas/rem/ultimo
            if not rows:
                try:
                    raw = http_get_json(
                        _ARD_REM_URL, timeout=10,
                        user_agent="balanz-monitor/1.0",
                        source="REM/ArgentinaDatos",
                    )
                    if isinstance(raw, list):
                        rows = self._normalize_ard(raw)
                        if rows:
                            logger.info("REM: fallback ArgentinaDatos OK (%d filas).", len(rows))
                except Exception as e:
                    logger.warning("REM fallback ArgentinaDatos falló: %s", e)

            if rows:
                type(self)._cache_rows     = rows
                type(self)._last_fetch_ts  = time.time()
                logger.info("Loaded %d REM IPC rows.", len(rows))

    @staticmethod
    def _safe_med(raw) -> Optional[float]:
        """Coerce a REM 'mediana' field to a fraction (decimal). None on bad input."""
        if raw is None:
            return None
        try:
            return float(raw) / 100.0
        except (TypeError, ValueError):
            return None

    def get_monthly_path(self) -> Dict[date, float]:
        """Mediana mensual REM por mes (decimal, e.g. 0.026 = 2.6%)."""
        self._fetch()
        out: Dict[date, float] = {}
        for row in self._cache_rows:
            if "mensual" not in str(row.get("referencia", "")).lower():
                continue
            d   = _parse_period(row.get("período"))
            med = self._safe_med(row.get("mediana"))
            if d is None or med is None:
                continue
            out[d] = med
        return out

    def get_next_12m_yoy(self) -> Optional[float]:
        """Mediana REM 'próx. 12 meses' interanual (decimal)."""
        self._fetch()
        for row in self._cache_rows:
            if "12 meses" in str(row.get("período", "")).lower():
                return self._safe_med(row.get("mediana"))
        return None

    def get_for_month(self, target: date) -> Optional[float]:
        """Mediana mensual REM para el mes que contiene `target` (decimal)."""
        path = self.get_monthly_path()
        for d, v in path.items():
            if d.year == target.year and d.month == target.month:
                return v
        return None
