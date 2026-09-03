"""USD/ARS quotes from dolarapi.com.

Architectural exception (like BCRAIndicesProvider for CER): FX reference
data is fetched from dolarapi.com which aggregates BCRA + market sources.
Used for:
  - Dolar mayorista (venta) -> deflator for DOLAR_LINKED bond TIRs
  - All quotes -> header strip in the web dashboard

Class-level cache shared across instances (DolarAPIProvider() is created
~8× per refresh cycle inside use_case.execute). TTL coalesces all those
calls into a single network round-trip. El refresco real lo hace `prefetch()`
(1× por ciclo del hub, sin TTL); `TTL_SECONDS` gatea SOLO el fallback síncrono
`_fetch()` — no tiene que ser menor que `settings.refresh_sec` (hoy 60 vs 5).
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
    # TTL de la cache de clase: coalesce todas las lecturas de un ciclo en un solo
    # round-trip. NADIE llama `invalidate_cache()` en la web (solo
    # scripts/data_quality_check.py), así que este TTL es el único gate del hot-path.
    TTL_SECONDS = 60

    # Negative caching (mismo patrón que REMProvider.FAIL_COOLDOWN / CAFCIProvider.
    # _last_fail_ts): con dolarapi caído y el TTL vencido, `get_quote()` se llama
    # POR INSTRUMENTO dentro del motor (~342 veces por ciclo vía DolarLinked/
    # HardDollar) y cada una salía a la red con timeout de 10s bajo el lock de
    # clase — el ciclo de 5s pasaba a decenas de minutos y bloqueaba también los
    # handlers sync (/header/cards). Tras un fallo no se reintenta hasta el cooldown.
    # "Fallo" incluye el 200 con payload inservible (ver `_process_payload`), no solo
    # la excepción de red: los dos dejan la cache sin refrescar y amplifican igual.
    FAIL_COOLDOWN = 30.0

    # Mayorista is the rate used to value DOLAR_LINKED bonds in pesos. If
    # the upstream stops updating we silently price DL bonds against an old
    # FX. Warn once per hour (not per fetch — would spam every 3s).
    _STALE_THRESHOLD_HOURS = 6
    _STALE_WARN_COOLDOWN_S = 3600

    _lock = threading.Lock()
    _cache: Dict[str, dict] = {}
    _last_fetch_ts: float = 0.0
    _last_fail_ts: float = 0.0
    _last_stale_warn_ts: float = 0.0

    def invalidate_cache(self) -> None:
        """Force the next get_* call to refetch from upstream. Único caller:
        `scripts/data_quality_check.py` (la web NO invalida: se apoya en el TTL).
        También limpia el sello de fallo para que "forzar" signifique forzar."""
        type(self)._last_fetch_ts = 0.0
        type(self)._last_fail_ts = 0.0

    async def prefetch(self, client) -> None:
        """Precarga asincrónica (FastAPI event loop)."""
        if self._cache and (time.time() - self._last_fetch_ts) < self.TTL_SECONDS:
            return
        try:
            payload = await client.get_json(self.URL, timeout=10.0, source="DolarAPI")
            self._process_payload(payload)
        except Exception as e:
            type(self)._last_fail_ts = time.time()
            logger.warning(f"DolarAPI prefetch failed: {e}")

    def _fetch(self) -> None:
        """Fetch sincrónico (fallback p/ scripts CLI)."""
        with self._lock:
            now = time.time()
            if self._cache and (now - self._last_fetch_ts) < self.TTL_SECONDS:
                return
            if self._last_fail_ts and (now - self._last_fail_ts) < self.FAIL_COOLDOWN:
                return  # upstream caído hace poco → servir stale, no amplificar
            try:
                # Fallback sincrónico directo sin retry (simplificado)
                resp = httpx.get(self.URL, timeout=10.0, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                self._process_payload(resp.json())
            except Exception as e:
                type(self)._last_fail_ts = time.time()
                logger.warning(f"DolarAPI fetch failed: {e}")

    def _process_payload(self, payload) -> bool:
        """Vuelca el payload a la cache de clase. Devuelve True si produjo al menos
        una cotización usable; False si el upstream contestó pero no sirvió nada."""
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
        if not fresh:
            # 200 con payload INUTILIZABLE (lista vacía, o filas sin `casa`): no hay
            # excepción que sellar, pero tampoco hay dato — sin este sello el
            # negative caching quedaba a medias y `get_quote()` volvía a la red POR
            # INSTRUMENTO (~342 por ciclo, medido: 50 lecturas = 50 GETs). Un
            # upstream degradado (mantenimiento, WAF que devuelve un body vacío)
            # amplificaba igual que uno caído. La cache vieja NO se toca: se sigue
            # sirviendo stale.
            type(self)._last_fail_ts = time.time()
            logger.warning(
                "DolarAPI respondió sin cotizaciones usables — cooldown %.0fs "
                "(se sigue sirviendo la cache previa: %d casas).",
                self.FAIL_COOLDOWN, len(self._cache),
            )
            return False
        type(self)._cache = fresh
        type(self)._last_fetch_ts = time.time()
        type(self)._last_fail_ts = 0.0
        logger.debug(f"Loaded {len(fresh)} USD quotes from dolarapi.")
        self._check_staleness()
        return True

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
