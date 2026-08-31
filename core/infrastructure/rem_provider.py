"""REM (Relevamiento de Expectativas de Mercado) del BCRA.

Fuente primaria: API pública externa
(endpoint de agregación comunitaria), que re-publica el dataset
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

import httpx

logger = logging.getLogger(__name__)

_BASE         = "https://bcra-rem-api.facujallia.workers.dev/api"
# OJO: host `api.` (el apex pelado argentinadatos.com da 404). Igual que el resto
# de read-paths ArgentinaDatos del repo (argentinadatos_provider / fci / feriados).
_ARD_REM_URL  = "https://api.argentinadatos.com/v1/finanzas/rem/ultimo"

# Mes abreviado inglés → nº (ARD usa 'May-26'). Tabla propia = locale-independiente
# (strptime '%b' depende de LC_TIME y rompería con un locale español).
_EN_MONTH = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
             "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


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
    # ARD: 'May-26' (mes-abrev inglés + yy). Día 1 (sólo importan año/mes).
    if len(s) == 6 and s[3] == "-" and s[:3].lower() in _EN_MONTH:
        try:
            return date(2000 + int(s[4:]), _EN_MONTH[s[:3].lower()], 1)
        except ValueError:
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

    FAIL_COOLDOWN = 120.0  # tras un fallo total (primaria+fallback), no reintentar
                           # hasta este cooldown → mata el storm de re-fetch por
                           # instrumento cuando la red/DNS se cae un instante.

    _lock          = threading.Lock()
    _cache_rows:   List[dict] = []
    _last_fetch_ts: float     = 0.0
    _last_fail_ts:  float     = 0.0

    @staticmethod
    def _normalize_ard(rows: list) -> List[dict]:
        """Normaliza la respuesta de ArgentinaDatos al schema interno.

        ARD `/finanzas/rem/ultimo` trae TODOS los indicadores del REM (IPC, TAMAR,
        export, PIB, ...) y DOS muestras ('todos' = total de participantes, 'top_10').
        Nos quedamos SOLO con **IPC nivel general · muestra=todos** — que replica la
        planilla `ipc_general` de la primaria — para no contaminar el sendero de
        inflación con TAMAR/otros ni duplicar con la muestra top_10.

        ARD usa 'periodo' (sin tilde), array directo (sin wrapper 'datos'). Mapeamos
        al schema que consumen get_monthly_path() / get_next_12m_yoy()."""
        out = []
        for row in rows:
            if "IPC nivel general" not in str(row.get("indicador") or ""):
                continue
            if row.get("muestra") != "todos":
                continue
            out.append({
                "período":   row.get("periodo", ""),   # tilde: compatible con _parse_period
                "referencia": row.get("referencia", ""),
                "mediana":   row.get("mediana"),
                "promedio":  row.get("promedio"),
            })
        return out

    def _fetch(self) -> None:
        with self._lock:
            now = time.time()
            if self._cache_rows and (now - self._last_fetch_ts) < self.TTL_SECONDS:
                return
            if (now - self._last_fail_ts) < self.FAIL_COOLDOWN:
                return

            rows: List[dict] = []

            # Primaria: facujallia.workers.dev
            try:
                resp = httpx.get(self.URL_IPC, timeout=10.0, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                payload = resp.json()
                rows = payload.get("datos") or []
            except Exception as e:
                logger.warning("REM primaria falló: %s — intentando ArgentinaDatos", e)

            # Fallback: ArgentinaDatos /v1/finanzas/rem/ultimo
            if not rows:
                try:
                    resp = httpx.get(_ARD_REM_URL, timeout=10.0, headers={"User-Agent": "balanz-monitor/1.0"})
                    resp.raise_for_status()
                    raw = resp.json()
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
            else:
                type(self)._last_fail_ts = time.time()

    async def prefetch(self, client) -> None:
        """Fetch asincrónico."""
        with self._lock:
            now = time.time()
            if self._cache_rows and (now - self._last_fetch_ts) < self.TTL_SECONDS:
                return
            if (now - self._last_fail_ts) < self.FAIL_COOLDOWN:
                return

        rows: List[dict] = []
        try:
            payload = await client.get_json(self.URL_IPC, timeout=10.0, source="REM/IPC")
            rows = payload.get("datos") or []
        except Exception as e:
            logger.warning("REM async primaria falló: %s — intentando ArgentinaDatos", e)

        if not rows:
            try:
                raw = await client.get_json(_ARD_REM_URL, timeout=10.0, source="REM/ArgentinaDatos")
                if isinstance(raw, list):
                    rows = self._normalize_ard(raw)
                    if rows:
                        logger.info("REM async: fallback ArgentinaDatos OK (%d filas).", len(rows))
            except Exception as e:
                logger.warning("REM async fallback ArgentinaDatos falló: %s", e)

        with self._lock:
            if rows:
                type(self)._cache_rows     = rows
                type(self)._last_fetch_ts  = time.time()
                logger.info("Loaded %d REM IPC rows (async).", len(rows))
            else:
                type(self)._last_fail_ts = time.time()

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
