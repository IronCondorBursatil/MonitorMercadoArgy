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

from core.infrastructure._tls import should_verify
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
# Parseo de paneles BYMA — PURO y CPU-bound (~10k filas × ~6,7us de pydantic).
# Vive module-level y sin `self` a propósito: `fetch()` lo corre en
# `asyncio.to_thread`, porque hacerlo inline bloqueaba ~67ms por ciclo el MISMO
# event loop que sirve el SSE y todos los requests HTTP del dashboard.
# --------------------------------------------------------------------------- #

# Un panel parseado: [(plazo, quote, bucket), ...] en el orden crudo de BYMA.
ParsedPanel = list[Tuple[str, Data912Row, str]]


def _parse_panel(data, bucket: str) -> ParsedPanel:
    """PURO: filas crudas de UN panel → [(plazo, Data912Row, bucket)]."""
    out: ParsedPanel = []
    for raw in data:
        q = byma_row_to_quote(raw)
        if q is None:
            continue
        out.append((settle_of(raw), q, bucket))
    return out


def _parse_panel_rows(buckets, results) -> list[ParsedPanel]:
    """PURO: parsea los N paneles y los devuelve POR SEPARADO (para cachearlos)."""
    return [_parse_panel(data, bucket) for bucket, data in zip(buckets, results)]


def _merge_panels(panels) -> FetchResult:
    """Vuelca los paneles parseados al snapshot por plazo + el mapa symbol→bucket.

    `panels` = [(filas_parseadas, es_fresco)] en el orden de `PANELS` (el último
    gana, igual que el doble-for original). Única excepción: una entrada CACHEADA
    (stale) no pisa un símbolo que ya escribió un panel fresco de este ciclo —
    `btnGeneral` puede solapar con `btnLideres`, que va a ciclo completo y no debe
    quedar servido con hasta 30s de atraso.
    """
    snaps = _empty_snaps()
    smap: SourceMap = {}
    fresh_keys: set = set()
    for rows, fresh in panels:
        for settle, q, bucket in rows:
            key = (settle, q.symbol)
            if not fresh and key in fresh_keys:
                continue
            snaps[settle][q.symbol] = q
            smap[q.symbol] = bucket
            if fresh:
                fresh_keys.add(key)
    return snaps, smap


def _parse_panels(buckets, results) -> FetchResult:
    """PURO: el doble-for original (parseo + merge) para el camino sin cache."""
    return _merge_panels([(p, True) for p in _parse_panel_rows(buckets, results)])


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

    # Paneles con TTL propio (`settings.equities_refresh_sec`): ningun panel del
    # monitor los consume en vivo — solo alimentan el reconcile de catalogo y el
    # sidebar del ABM — y son los dos POST mas pesados. `btnLideres` queda a ciclo
    # completo (lo usa `panel_lider`), igual que los 3 de renta fija.
    SLOW_PANELS = frozenset({"btnGeneral", "btnCedears"})

    def __init__(self) -> None:
        # flag → (filas YA parseadas del panel, monotonic del POST). Guardamos el
        # parseo, no el crudo: asi el hit de cache tampoco paga pydantic.
        self._panel_cache: Dict[str, Tuple[ParsedPanel, float]] = {}

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
        now = time.monotonic()
        ttl = max(0.0, float(getattr(settings, "equities_refresh_sec", 0) or 0))
        cached: Dict[int, ParsedPanel] = {}
        pending: list = []
        for i, (flag, _seg, _plazos, _bucket) in enumerate(self.PANELS):
            hit = self._panel_cache.get(flag) if (ttl and flag in self.SLOW_PANELS) else None
            if hit is not None and (now - hit[1]) < ttl:
                cached[i] = hit[0]
            else:
                pending.append(i)

        results = await asyncio.gather(
            *[self._panel(client, *self.PANELS[i][:3]) for i in pending]
        )
        # Parseo pydantic FUERA del event loop (ver `_parse_panel_rows`).
        parsed = await asyncio.to_thread(
            _parse_panel_rows, [self.PANELS[i][3] for i in pending], results)

        fresh = dict(zip(pending, parsed))
        for i, rows in fresh.items():
            flag = self.PANELS[i][0]
            # Un panel VACIO (POST caido, breaker abierto) NO se cachea: hacerlo
            # serviria un panel en blanco durante todo el TTL en vez de reintentar.
            if flag in self.SLOW_PANELS and rows:
                self._panel_cache[flag] = (rows, now)
        # Los paneles cacheados se re-mergean SIEMPRE: si desaparecieran del
        # snapshot, el floor Data912 y la ventana K del hub verian una fuente que
        # "dejo de listar" esos simbolos.
        return _merge_panels([
            (fresh[i], True) if i in fresh else (cached[i], False)
            for i in range(len(self.PANELS))
        ])


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

    # (endpoint, body flag, plazos, bucket). `getIceEquity` es el VERDADERO endpoint de
    # market-data del addin: honra TODOS los flags de panel (btnLideres/General/Cedears/
    # TitPublicos/Letras/ObligNegociables) y devuelve precios vivos. `getLeadingEquity`
    # —usado antes para todo salvo CEDEARs— devuelve solo el CATÁLOGO: el esqueleto de la
    # especie con TODOS los campos de precio en 0 (trade/closing/previousClosing/bid/ask/
    # settlementPrice), así que el board quedaba en blanco (verificado en vivo 2026-06: 0
    # de 4400 títulos públicos con precio vía getLeadingEquity vs 1036 vía getIceEquity).
    # El dedicado `getObligacionesNegociables` 400ea ("No se pudo determinar el panel").
    # Renta fija pide T0+T1 (CI + 24hs juntos).
    PANELS = [
        ("/excel/byma/data/getIceEquity", {"btnLideres": True}, ("T1",), "stocks"),
        ("/excel/byma/data/getIceEquity", {"btnGeneral": True}, ("T1",), "stocks"),
        ("/excel/byma/data/getIceEquity", {"btnCedears": True}, ("T1",), "cedears"),
        ("/excel/byma/data/getIceEquity", {"btnTitPublicos": True}, ("T0", "T1"), "bonds"),
        ("/excel/byma/data/getIceEquity", {"btnLetras": True}, ("T0", "T1"), "notes"),
        ("/excel/byma/data/getIceEquity", {"btnObligNegociables": True}, ("T0", "T1"), "corp"),
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
            # TLS: por acá viajan usuario y contraseña del usuario (grant_type=
            # password) + el Basic del client, así que la verificación va por la
            # política única del repo (_tls.should_verify) en vez de un
            # `verify=False` hardcodeado. Hoy www.bymadata.com.ar NO está en la
            # allowlist de cadena-rota → verifica (chequeado en vivo con trust
            # store certifi-only: el handshake completa y el server responde).
            async with httpx.AsyncClient(verify=should_verify(self.TOKEN_URL),
                                         timeout=15.0) as h:
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
        # Los 6 paneles comparten `getIceEquity` y se distinguen SÓLO por el flag del
        # body, así que la etiqueta lleva el flag: con el endpoint solo, el throttle de
        # 60s colapsaba en una clave única (el primer panel que falla silencia a los
        # otros cinco) y el mensaje no decía cuál se cayó.
        panel = "+".join(sorted(body)) or "?"
        tag = f"{endpoint}[{panel}]"
        try:
            resp = await client.post_json(self.API_BASE + endpoint, json=payload,
                                          headers=self._bearer(token), source=f"BYMArt{tag}")
            return _rows(resp)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:  # token vencido → relogin + 1 reintento
                token = await self._ensure_token(stale_token=token)
                try:
                    resp = await client.post_json(self.API_BASE + endpoint, json=payload,
                                                  headers=self._bearer(token), source=f"BYMArt{tag}")
                    return _rows(resp)
                except Exception as e2:  # noqa: BLE001
                    _warn_throttled(f"rt{tag}", "BYMArt%s retry failed: %s", tag, e2)
                    return []
            _warn_throttled(f"rt{tag}", "BYMArt%s fetch failed: %s", tag, e)
            return []
        except CircuitOpenError:
            return []
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _warn_throttled(f"rt{tag}", "BYMArt%s fetch failed: %s: %s",
                            tag, type(e).__name__, e)
            return []

    async def fetch(self, client: ResilientClient) -> FetchResult:
        token = await self._ensure_token()
        results = await asyncio.gather(
            *[self._panel(client, ep, body, plazos, token) for ep, body, plazos, _ in self.PANELS]
        )
        # Parseo pydantic FUERA del event loop (ver `_parse_panels`).
        return await asyncio.to_thread(
            _parse_panels, [bucket for _, _, _, bucket in self.PANELS], results)


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
