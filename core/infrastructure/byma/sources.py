"""Fuentes de cotizaciones live (hot-path). Una `MarketSource` produce
`{symbol: Data912Row}` + `{symbol: bucket}` y el `ProviderHub` la mergea stale-safe.

Tres implementaciones intercambiables en runtime:
  - `Data912Source`     : la lógica histórica (4 endpoints data912.com).
  - `BymaOpenSource`    : open.bymadata.com.ar (público, ~20min demora, sin clave).
  - `BymaRealtimeSource`: addin.bymadata.com.ar (OAuth con clave del Add-In, vivo).

El `bucket` (`bonds`/`notes`/`corp`/`stocks`/`cedears`) replica el origen de Data912
para que el reconcile de catálogo y el sidebar del ABM sigan andando sin cambios.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Dict, Optional, Protocol, Tuple

import httpx

from config.settings import settings

from core.infrastructure.async_http import ResilientClient
from core.infrastructure.byma.field_map import SETTLE_24, SETTLE_CI, byma_row_to_quote, settle_of
from core.infrastructure.circuit_breaker import CircuitOpenError
from core.infrastructure.schemas import Data912Row, parse_snapshot_rows

logger = logging.getLogger(__name__)

# Throttle de warnings: una falla persistente (panel 400, geo-block, breaker) NO
# debe inundar la consola cada ciclo (~5-12s). Logueamos a lo sumo 1×/min por clave.
_WARN_INTERVAL_S = 60.0
_warn_last: Dict[str, float] = {}


def _warn_throttled(key: str, msg: str, *args) -> None:
    now = time.monotonic()
    last = _warn_last.get(key)
    if last is None or (now - last) >= _WARN_INTERVAL_S:
        _warn_last[key] = now
        logger.warning(msg, *args)

QuoteMap = Dict[str, Data912Row]
SourceMap = Dict[str, str]
# Snapshots por plazo: {"24": {symbol: row}, "CI": {symbol: row}}.
SettleSnaps = Dict[str, QuoteMap]
FetchResult = Tuple[SettleSnaps, SourceMap]


def _empty_snaps() -> SettleSnaps:
    return {SETTLE_24: {}, SETTLE_CI: {}}


class MarketSource(Protocol):
    mode: str
    label: str
    delayed: bool

    async def fetch(self, client: ResilientClient) -> FetchResult: ...


def _rows(resp) -> list:
    """Lista `data` de una respuesta paginada BYMA, o la respuesta si ya es lista."""
    if isinstance(resp, dict) and "data" in resp:
        return resp.get("data") or []
    return resp if isinstance(resp, list) else []


# --------------------------------------------------------------------------- #
# Data912 (fallback) — la lógica que vivía en ProviderHub.fetch_data912.
# --------------------------------------------------------------------------- #

class Data912Source:
    mode = "data912"
    label = "Data912"
    delayed = False

    # 4 paneles live (las opciones van por el fetch dedicado de la chain).
    ENDPOINTS = {
        "notes": "https://data912.com/live/arg_notes",
        "bonds": "https://data912.com/live/arg_bonds",
        "corp": "https://data912.com/live/arg_corp",
        "stocks": "https://data912.com/live/arg_stocks",
    }

    async def fetch(self, client: ResilientClient) -> FetchResult:
        async def _one(name: str, url: str):
            try:
                payload = await client.get_json(url, source=f"Data912/{name}")
                return name, parse_snapshot_rows(payload if isinstance(payload, list) else [])
            except CircuitOpenError:
                return name, {}
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                _warn_throttled(f"data912/{name}", "Data912/%s fetch failed: %s: %s",
                                name, type(e).__name__, e)
                return name, {}

        results = await asyncio.gather(*[_one(n, u) for n, u in self.ENDPOINTS.items()])
        rows: QuoteMap = {}
        smap: SourceMap = {}
        for name, r in results:
            for sym in r:
                smap[sym] = name
            rows.update(r)
        # Data912 no distingue plazo → CI = 24hs (mismo precio en ambos).
        return {SETTLE_24: rows, SETTLE_CI: dict(rows)}, smap


# --------------------------------------------------------------------------- #
# BYMA open (default) — open.bymadata.com.ar, público, ~20 min demora.
# --------------------------------------------------------------------------- #

class BymaOpenSource:
    mode = "byma_open"
    label = "BYMA open (20m)"
    delayed = True

    BASE = "https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free"
    # Clave pública de la app (del bundle del portal; ver GUIA_BYMADATA.md §3).
    APP_TOKEN = "dc826d4c2dde7519e882a250359a23a7"

    # (flag de panel, segmento header `Options`, plazos a pedir, bucket lógico).
    # Renta fija pide T0+T1 en UNA llamada → devuelve CI y 24hs juntos (se rutea por
    # settlementType). Acciones/CEDEARs solo 24hs (T1). Verificado en vivo.
    PANELS = [
        ("btnLideres", "renta-variable", ("T1",), "stocks"),
        ("btnGeneral", "renta-variable", ("T1",), "stocks"),
        ("btnCedears", "renta-variable", ("T1",), "cedears"),
        ("btnTitPublicos", "renta-fija", ("T0", "T1"), "bonds"),
        ("btnLetras", "renta-fija", ("T0", "T1"), "notes"),
        ("btnObligNegociables", "renta-fija", ("T0", "T1"), "corp"),
    ]

    def _headers(self, segment: str) -> dict:
        return {
            "Token": self.APP_TOKEN,
            "Options": segment,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://open.bymadata.com.ar",
            "Referer": "https://open.bymadata.com.ar/",
            "User-Agent": "Mozilla/5.0",
        }

    async def _panel(self, client: ResilientClient, flag: str, segment: str, plazos) -> list:
        body = {"excludeZeroPxAndQty": False, "page_number": 1, "page_size": 5000, flag: True}
        for p in plazos:
            body[p] = True
        try:
            resp = await client.post_json(
                self.BASE + "/get-market-data", json=body,
                headers=self._headers(segment), source=f"BYMAopen/{flag}")
            return _rows(resp)
        except CircuitOpenError:
            logger.debug("BYMAopen/%s breaker OPEN; usando stale", flag)
            return []
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _warn_throttled(f"open/{flag}", "BYMAopen/%s fetch failed: %s: %s",
                            flag, type(e).__name__, e)
            return []

    async def fetch(self, client: ResilientClient) -> FetchResult:
        results = await asyncio.gather(
            *[self._panel(client, flag, seg, plazos) for flag, seg, plazos, _ in self.PANELS]
        )
        snaps = _empty_snaps()
        smap: SourceMap = {}
        for (flag, seg, plazos, bucket), data in zip(self.PANELS, results):
            for raw in data:
                q = byma_row_to_quote(raw)
                if q is None:
                    continue
                snaps[settle_of(raw)][q.symbol] = q
                smap[q.symbol] = bucket
        return snaps, smap


# --------------------------------------------------------------------------- #
# BYMA realtime — addin.bymadata.com.ar, OAuth con clave del Add-In, vivo.
# --------------------------------------------------------------------------- #

class BymaRealtimeError(RuntimeError):
    pass


class BymaRealtimeSource:
    mode = "byma_realtime"
    label = "BYMA tiempo real"
    delayed = False

    TOKEN_URL = "https://www.bymadata.com.ar/generic-oauth-core/oauth/token"
    API_BASE = "https://addin.bymadata.com.ar/vanoms-be-core/rest/api"
    # client del addin (embebidos en el .xll; ver GUIA_BYMADATA.md §4).
    # Defaults desde settings (centralizado/override por env); NO secreto — ver
    # settings.byma_client_secret. Las credenciales del usuario van por .env.
    CLIENT_ID = settings.byma_client_id
    CLIENT_SECRET = settings.byma_client_secret

    # (endpoint, body flag, plazos, bucket). `getLeadingEquity` es en realidad el
    # endpoint general de market-data del addin: toma TODOS los flags de panel
    # (btnLideres/General/TitPublicos/Letras/ObligNegociables), igual que
    # `/get-market-data` en la open. El dedicado `getObligacionesNegociables` 400ea
    # ("No se pudo determinar el panel") → las ONs van por getLeadingEquity. CEDEARs
    # usan su feed propio (getIceEquity). Renta fija pide T0+T1 (CI + 24hs juntos).
    PANELS = [
        ("/excel/byma/data/getLeadingEquity", {"btnLideres": True}, ("T1",), "stocks"),
        ("/excel/byma/data/getLeadingEquity", {"btnGeneral": True}, ("T1",), "stocks"),
        ("/excel/byma/data/getIceEquity", {"btnCedears": True}, ("T1",), "cedears"),
        ("/excel/byma/data/getLeadingEquity", {"btnTitPublicos": True}, ("T0", "T1"), "bonds"),
        ("/excel/byma/data/getLeadingEquity", {"btnLetras": True}, ("T0", "T1"), "notes"),
        ("/excel/byma/data/getLeadingEquity", {"btnObligNegociables": True}, ("T0", "T1"), "corp"),
    ]

    def __init__(self, username: Optional[str] = None, password: Optional[str] = None,
                 client_id: Optional[str] = None, client_secret: Optional[str] = None):
        self.username = username or os.environ.get("BYMADATA_USER")
        self.password = password or os.environ.get("BYMADATA_PASS")
        self.client_id = client_id or os.environ.get("BYMADATA_CLIENT_ID", self.CLIENT_ID)
        self.client_secret = client_secret or os.environ.get("BYMADATA_CLIENT_SECRET", self.CLIENT_SECRET)
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def has_credentials() -> bool:
        return bool(os.environ.get("BYMADATA_USER") and os.environ.get("BYMADATA_PASS"))

    async def _ensure_token(self, stale_token: Optional[str] = None) -> str:
        async with self._lock:
            # Token válido y distinto del que el caller vio vencer (en un 401, otro
            # panel ya relogueó) → reusar. Colapsa el "thundering herd" de N logins
            # cuando los N paneles reciben 401 a la vez.
            if self._token and self._token != stale_token and time.monotonic() < self._expires_at:
                return self._token
            if not self.username or not self.password:
                raise BymaRealtimeError(
                    "Faltan credenciales: definí BYMADATA_USER / BYMADATA_PASS en el entorno (.env).")
            basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            # Login infrecuente (token dura ~24h) → cliente httpx propio; el host
            # del token (www.bymadata.com.ar) no está geo-bloqueado.
            async with httpx.AsyncClient(verify=False, timeout=15.0) as h:
                r = await h.post(
                    self.TOKEN_URL,
                    data={"grant_type": "password", "username": self.username,
                          "password": self.password},
                    headers={"Authorization": f"Basic {basic}",
                             "Content-Type": "application/x-www-form-urlencoded"})
                if r.status_code != 200:
                    raise BymaRealtimeError(f"OAuth token {r.status_code}: {r.text[:200]}")
                tok = r.json()
            self._token = tok["access_token"]
            self._expires_at = time.monotonic() + int(tok.get("expires_in", 3600)) - 60
            logger.info("BYMADATA realtime login OK (expira en %ss).", tok.get("expires_in"))
            return self._token

    def _bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Accept": "application/json",
                "Content-Type": "application/json"}

    async def _panel(self, client: ResilientClient, endpoint: str, body: dict,
                     plazos, token: str) -> list:
        payload = {"excludeZeroPxAndQty": False, **{p: True for p in plazos}, **body}
        try:
            resp = await client.post_json(self.API_BASE + endpoint, json=payload,
                                          headers=self._bearer(token), source=f"BYMArt{endpoint}")
            return _rows(resp)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:  # token vencido → relogin + 1 reintento
                token = await self._ensure_token(stale_token=token)
                try:
                    resp = await client.post_json(self.API_BASE + endpoint, json=payload,
                                                  headers=self._bearer(token), source=f"BYMArt{endpoint}")
                    return _rows(resp)
                except Exception as e2:  # noqa: BLE001
                    _warn_throttled(f"rt{endpoint}", "BYMArt%s retry failed: %s", endpoint, e2)
                    return []
            _warn_throttled(f"rt{endpoint}", "BYMArt%s fetch failed: %s", endpoint, e)
            return []
        except CircuitOpenError:
            return []
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _warn_throttled(f"rt{endpoint}", "BYMArt%s fetch failed: %s: %s",
                            endpoint, type(e).__name__, e)
            return []

    async def fetch(self, client: ResilientClient) -> FetchResult:
        token = await self._ensure_token()
        results = await asyncio.gather(
            *[self._panel(client, ep, body, plazos, token) for ep, body, plazos, _ in self.PANELS]
        )
        snaps = _empty_snaps()
        smap: SourceMap = {}
        for (ep, body, plazos, bucket), data in zip(self.PANELS, results):
            for raw in data:
                q = byma_row_to_quote(raw)
                if q is None:
                    continue
                snaps[settle_of(raw)][q.symbol] = q
                smap[q.symbol] = bucket
        return snaps, smap


# --------------------------------------------------------------------------- #
# Registry: mode → factory. Default según settings.market_source.
# --------------------------------------------------------------------------- #

_SOURCES = {
    Data912Source.mode: Data912Source,
    BymaOpenSource.mode: BymaOpenSource,
    BymaRealtimeSource.mode: BymaRealtimeSource,
}

MODES = (BymaOpenSource.mode, BymaRealtimeSource.mode, Data912Source.mode)


def make_source(mode: str) -> MarketSource:
    """Instancia la fuente del modo dado. `byma_realtime` exige credenciales."""
    cls = _SOURCES.get(mode)
    if cls is None:
        raise ValueError(f"market source desconocido: {mode!r} (válidos: {MODES})")
    if cls is BymaRealtimeSource and not BymaRealtimeSource.has_credentials():
        raise BymaRealtimeError(
            "BYMA tiempo real requiere BYMADATA_USER / BYMADATA_PASS en el entorno (.env).")
    return cls()


def source_label(mode: str) -> str:
    cls = _SOURCES.get(mode)
    return cls.label if cls else mode
