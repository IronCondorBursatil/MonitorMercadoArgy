"""USD/ARS quotes from dolarapi.com.

Architectural exception (like BCRAIndicesProvider for CER): FX reference
data is fetched from dolarapi.com which aggregates BCRA + market sources.
Used for:
  - Dolar mayorista (venta) -> deflator for DOLAR_LINKED bond TIRs
  - All quotes -> header strip in the web dashboard

Class-level cache shared across instances (DolarAPIProvider() is created
~8× per refresh cycle inside use_case.execute). TTL coalesces all those
calls into a single network round-trip while keeping data fresh between
server cycles — must stay strictly below REFRESH_SEC.
"""

import httpx
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class DolarAPIProvider:
    URL = "https://dolarapi.com/v1/dolares"
    # Backstop TTL for callers that never invalidate (CLI scripts). The web
    # refresh loop invalidates explicitly via `invalidate_cache()` once per
    # cycle so every panel inside the cycle shares one network round-trip
    # even when the cycle exceeds this TTL.
    TTL_SECONDS = 60

    # Mayorista is the rate used to value DOLAR_LINKED bonds in pesos. If
    # the upstream stops updating we silently price DL bonds against an old
    # FX. Warn once per hour (not per fetch — would spam every 3s).
    _STALE_THRESHOLD_HOURS = 6
    _STALE_WARN_COOLDOWN_S = 3600

    _lock = threading.Lock()
    _cache: Dict[str, dict] = {}
    _last_fetch_ts: float = 0.0
    _last_stale_warn_ts: float = 0.0

    def invalidate_cache(self) -> None:
        """Force the next get_* call to refetch from upstream. Called by the
        refresh loop at the start of each cycle."""
        type(self)._last_fetch_ts = 0.0

    async def prefetch(self, client) -> None:
        """Precarga asincrónica (FastAPI event loop)."""
        if self._cache and (time.time() - self._last_fetch_ts) < self.TTL_SECONDS:
            return
        try:
            payload = await client.get_json(self.URL, timeout=10.0, source="DolarAPI")
            self._process_payload(payload)
        except Exception as e:
            logger.warning(f"DolarAPI prefetch failed: {e}")

    def _fetch(self) -> None:
        """Fetch sincrónico (fallback p/ scripts CLI)."""
        with self._lock:
            if self._cache and (time.time() - self._last_fetch_ts) < self.TTL_SECONDS:
                return
            try:
                # Fallback sincrónico directo sin retry (simplificado)
                resp = httpx.get(self.URL, timeout=10.0, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                self._process_payload(resp.json())
            except Exception as e:
                logger.warning(f"DolarAPI fetch failed: {e}")

    def _process_payload(self, payload) -> None:
        fresh = {}
        for row in payload:
            casa = str(row.get("casa", "")).strip().lower()
            if not casa:
                continue
            fresh[casa] = {
                "nombre": row.get("nombre"),
                "compra": float(row["compra"]) if row.get("compra") else None,
                "venta": float(row["venta"]) if row.get("venta") else None,
                "fechaActualizacion": row.get("fechaActualizacion"),
            }
        if fresh:
            type(self)._cache = fresh
            type(self)._last_fetch_ts = time.time()
            logger.debug(f"Loaded {len(fresh)} USD quotes from dolarapi.")
            self._check_staleness()

    def _check_staleness(self) -> None:
        # Skip on weekends: the FX market doesn't trade Sat/Sun, so a quote
        # from Friday's close registers as 40-60h "stale" on Sunday — that's
        # normal, not an anomaly. A Monday morning warning with the same gap
        # would be real. Holidays still trigger false positives but are rare
        # enough that a hardcoded calendar isn't worth maintaining.
        if datetime.now().weekday() >= 5:
            return
        mayorista = self._cache.get("mayorista")
        if not mayorista:
            return
        raw = mayorista.get("fechaActualizacion")
        if not raw:
            return
        try:
            t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return
        try:
            age = datetime.now(t.tzinfo) - t
        except TypeError:
            return
        if age <= timedelta(hours=self._STALE_THRESHOLD_HOURS):
            return
        now = time.time()
        if now - self._last_stale_warn_ts < self._STALE_WARN_COOLDOWN_S:
            return
        type(self)._last_stale_warn_ts = now
        hours = age.total_seconds() / 3600
        logger.warning(
            "FX mayorista stale: dolarapi.com last update %.1fh ago (>%dh threshold). "
            "DOLAR_LINKED bond valuations use this quote — verify upstream.",
            hours, self._STALE_THRESHOLD_HOURS,
        )

    def get_all(self) -> Dict[str, dict]:
        self._fetch()
        return dict(self._cache)

    def get_quote(self, casa: str) -> Optional[dict]:
        self._fetch()
        return self._cache.get(casa.lower())

    def get_mayorista_venta(self) -> Optional[float]:
        q = self.get_quote("mayorista")
        return q.get("venta") if q else None

    def get_mep_venta(self) -> Optional[float]:
        """Offer (venta) del dólar MEP = casa 'bolsa' en dolarapi. Usado para pasar
        la pata pesos (…O) de una ON hard-dollar LEY ARGENTINA a su USD implícito."""
        q = self.get_quote("bolsa")
        return q.get("venta") if q else None

    def get_ccl_venta(self) -> Optional[float]:
        """Offer (venta) del dólar CCL/cable = casa 'contadoconliqui'. Usado para la
        pata pesos (…O) de una ON hard-dollar LEY EXTRANJERA (o sin ley declarada)."""
        q = self.get_quote("contadoconliqui")
        return q.get("venta") if q else None

    def get_mayorista_mid(self) -> Optional[float]:
        """Mid del dólar mayorista = (compra + venta) / 2. Usado como spot
        para la TNA implícita de los futuros DLR — representa mejor el "spot
        operable" que el A3500 (índice de referencia BCRA, no transable).
        Fallback a venta si falta compra."""
        q = self.get_quote("mayorista")
        if not q:
            return None
        c, v = q.get("compra"), q.get("venta")
        if c and v:
            return (c + v) / 2.0
        return v or c
