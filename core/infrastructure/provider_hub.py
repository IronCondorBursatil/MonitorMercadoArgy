"""ProviderHub: ingesta async coordinada (deuda #6).

`refresh_all()` delega en la **fuente activa** (`MarketSource`: BYMA open / BYMA
realtime / Data912), trae sus paneles en paralelo, valida (Pydantic) y mergea SÓLO
lo bueno: si todo falla, preserva el último snapshot bueno (no wipea el cache).
La fuente se cambia en runtime con `set_source()` (hot-swap sin reiniciar los loops:
`HubMarketDataProvider` lee `snapshot()` en vivo cada ciclo).

Las opciones se traen del panel BYMA open `/options` por defecto (`fetch_options`,
con OI real + underlyingSymbol/optionType/maturityDate autoritativos; Data912 de
fallback) a un snapshot aparte que consume la chain — independiente de la fuente
live activa. Los subyacentes salen del feed live del hub.

BCRA / DolarAPI / ArgentinaDatos / CAFCI se integran al hub junto con la
reescritura async de esos providers (Fase 4).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import date
from typing import Dict, List, Optional

from core.domain.interfaces import IMarketDataProvider
from core.domain.models import MarketSnapshot
from core.infrastructure.async_http import ResilientClient
from core.infrastructure.byma.field_map import SETTLE_24, SETTLE_CI
from core.infrastructure.byma.sources import Data912Source, MarketSource
from core.infrastructure.circuit_breaker import CircuitOpenError
from core.infrastructure.schemas import Data912Row, parse_snapshot_rows

logger = logging.getLogger(__name__)


def _has_rows(snaps: Dict[str, Dict[str, Data912Row]]) -> bool:
    """¿La fuente trajo algo en algún plazo este ciclo?"""
    return any(bool(rows) for rows in (snaps or {}).values())


class ProviderHub:
    # Opciones Data912 (fallback de BYMA /options). Incluye `stocks` para tener el
    # subyacente consistente con las opciones cuando se usa este camino.
    OPTIONS_ENDPOINTS = {
        "options": "https://data912.com/live/arg_options",
        "stocks": "https://data912.com/live/arg_stocks",
    }

    def __init__(self, client: ResilientClient, active_source: Optional[MarketSource] = None):
        self._client = client
        # Snapshot por plazo (stale-safe): {"24": {sym:row}, "CI": {sym:row}}.
        self._snap: Dict[str, Dict[str, Data912Row]] = {SETTLE_24: {}, SETTLE_CI: {}}
        self._source: Dict[str, str] = {}           # symbol → bucket (bonds/notes/corp/stocks/cedears)
        self._opt_snapshot: Dict[str, Data912Row] = {}
        self._opt_source: Dict[str, str] = {}
        # Lock de hilos: el snapshot lo MUTA el event loop (refresh_all) y lo LEE
        # el thread pool (use_case.execute vía to_thread → snapshot()). Sin esto,
        # dict(self._snapshot) puede pegar "dictionary changed size during iteration".
        # (threading.Lock, NO asyncio.Lock: el reader corre en otro hilo.)
        self._lock = threading.Lock()
        # Default Data912 (back-compat de tests/CLI); la app setea byma_open al arrancar.
        self._active: MarketSource = active_source or Data912Source()

    # ---------------- fuente activa (hot-swap) ----------------
    @property
    def active_mode(self) -> str:
        return self._active.mode

    @property
    def active_label(self) -> str:
        return self._active.label

    @property
    def is_delayed(self) -> bool:
        return bool(getattr(self._active, "delayed", False))

    def set_source(self, source: MarketSource) -> None:
        """Cambia la fuente live. El próximo `refresh_all()` ya usa la nueva."""
        logger.info("ProviderHub: fuente activa → %s", source.mode)
        self._active = source

    # ---------------- fetch live (fuente activa) ----------------
    async def refresh_all(self) -> Dict[str, Data912Row]:
        """Trae las cotizaciones live de la fuente activa (paneles en paralelo),
        mergea stale-safe por plazo y devuelve el snapshot 24hs. Punto de extensión
        para FX/BCRA."""
        try:
            snaps, source = await self._active.fetch(self._client)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — la fuente caída no tumba el ciclo
            logger.warning("market source %s fetch failed: %s: %s",
                           self._active.mode, type(e).__name__, e)
            snaps, source = {}, {}
        # Backstop pre-market: si la fuente activa no trajo NADA y el snapshot sigue
        # en blanco (típico de BYMA open antes de las 10:30 ART — devuelve empty:true,
        # y con su demora de ~20m las filas vivas recién entran ~10:50 — o
        # findes/feriados/outage), primamos con el cierre previo de Data912 para no
        # dejar el board en 0.00. Solo MIENTRAS no hay datos: apenas la fuente activa
        # entrega su primer ciclo, el merge stale-safe la deja a cargo y no volvemos a
        # caer acá (no pisamos precios vivos con cierre previo en un vacío transitorio).
        if (not _has_rows(snaps) and self._active.mode != Data912Source.mode
                and self._snapshot_empty()):
            try:
                d_snaps, d_source = await Data912Source().fetch(self._client)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("backstop Data912 falló: %s: %s", type(e).__name__, e)
            else:
                if _has_rows(d_snaps):
                    logger.info("fuente %s sin datos y board vacío; backstop Data912 "
                                "(%d símbolos, cierre previo).",
                                self._active.mode, len(d_snaps.get(SETTLE_24, {})))
                    snaps, source = d_snaps, d_source
        return self._merge(snaps, source)

    def _snapshot_empty(self) -> bool:
        """True si todavía no acumulamos ninguna cotización (board en blanco)."""
        with self._lock:
            return not any(self._snap.get(s) for s in (SETTLE_24, SETTLE_CI))

    async def fetch_data912(self) -> Dict[str, Data912Row]:
        """Atajo: fuerza un fetch Data912 a este hub (usado por tests/CLI). NO
        cambia la fuente activa."""
        snaps, source = await Data912Source().fetch(self._client)
        return self._merge(snaps, source)

    def _merge(self, snaps: Dict[str, Dict[str, Data912Row]], source: Dict[str, str]) -> Dict[str, Data912Row]:
        # Preservar stale por plazo (no wipear → el UI no queda en blanco).
        with self._lock:
            for settle, rows in (snaps or {}).items():
                if rows:
                    self._snap.setdefault(settle, {}).update(rows)
            if source:
                self._source.update(source)
            return dict(self._snap[SETTLE_24])

    # ---------------- opciones (BYMA open por defecto, Data912 fallback) ------
    # Snapshot aparte (`_opt_snapshot`/`_opt_source`), independiente de la fuente
    # live activa de los paneles. BYMA da OI real + underlyingSymbol/optionType/
    # maturityDate autoritativos (roots-independiente → más profundidad); Data912
    # queda de fallback. Los subyacentes (spots) salen del feed live del hub.
    BYMA_OPTIONS_URL = (
        "https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free/options")
    _BYMA_OPT_HEADERS = {
        "User-Agent": "Mozilla/5.0", "Accept": "application/json",
        "Content-Type": "application/json",
    }

    async def fetch_options(self, source: str = "byma") -> "tuple[Dict[str, Data912Row], Dict[str, Data912Row]]":
        """Devuelve `(options_rows, stocks_rows)` para `build_options`.

        `source='byma'` usa el panel BYMA open `/options` (sin token) y toma los
        subyacentes del snapshot live del hub; cae a Data912 si BYMA no trae nada
        o si todavía no hay stocks live. `source='data912'` fuerza el camino previo."""
        if source == "byma":
            await self._fetch_options_byma()
            snap, src = self.options_snapshot(), self.options_sources()
            opt = {s: r for s, r in snap.items() if src.get(s) == "options"}
            stk = {s: r for s, r in self.snapshot().items()
                   if self.sources().get(s) == "stocks"}
            if opt and stk:
                return opt, stk
            logger.info("opciones BYMA sin datos/subyacentes; fallback a Data912.")
        await self.fetch_options_data912()
        snap, src = self.options_snapshot(), self.options_sources()
        return ({s: r for s, r in snap.items() if src.get(s) == "options"},
                {s: r for s, r in snap.items() if src.get(s) == "stocks"})

    async def _fetch_options_byma(self) -> Dict[str, Data912Row]:
        """Trae el panel BYMA open `/options` → mergea en `_opt_snapshot` las filas
        que son opciones reales (traen `underlyingSymbol`). Stale-safe (acumula).
        Devuelve lo traído ESTE ciclo (puede ser {} ante fallo transitorio)."""
        from core.infrastructure.byma.field_map import byma_row_to_quote
        try:
            resp = await self._client.post_json(
                self.BYMA_OPTIONS_URL, json={}, headers=self._BYMA_OPT_HEADERS,
                source="BYMAopen/options")
        except CircuitOpenError:
            return {}
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — la fuente caída no tumba el ciclo
            logger.warning("BYMAopen/options fetch failed: %s: %s", type(e).__name__, e)
            return {}
        rows = resp.get("data") if isinstance(resp, dict) else resp
        merged: Dict[str, Data912Row] = {}
        for raw in (rows if isinstance(rows, list) else []):
            q = byma_row_to_quote(raw)
            if q is not None and q.opt_underlying:   # opción real (no acción/total)
                merged[q.symbol] = q
        with self._lock:
            if merged:
                self._opt_snapshot.update(merged)
                for s in merged:
                    self._opt_source[s] = "options"
        return merged

    async def fetch_options_data912(self) -> Dict[str, Data912Row]:
        async def _one(name: str, url: str):
            try:
                payload = await self._client.get_json(url, source=f"Data912/{name}")
                return name, parse_snapshot_rows(payload if isinstance(payload, list) else [])
            except CircuitOpenError:
                return name, {}
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("Data912/%s fetch failed: %s: %s", name, type(e).__name__, e)
                return name, {}

        results = await asyncio.gather(*[_one(n, u) for n, u in self.OPTIONS_ENDPOINTS.items()])
        merged: Dict[str, Data912Row] = {}
        source: Dict[str, str] = {}
        for name, rows in results:
            for sym in rows:
                source[sym] = name
            merged.update(rows)
        with self._lock:
            if merged:
                self._opt_snapshot.update(merged)
                self._opt_source.update(source)
            return dict(self._opt_snapshot)

    def options_snapshot(self) -> Dict[str, Data912Row]:
        with self._lock:
            return dict(self._opt_snapshot)

    def options_sources(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._opt_source)

    # ---------------- accessors ----------------
    def sources(self) -> Dict[str, str]:
        """{symbol: bucket} del último snapshot (para el listado de faltantes del ABM)."""
        with self._lock:
            return dict(self._source)

    def snapshot(self, settle: str = SETTLE_24) -> Dict[str, Data912Row]:
        """Snapshot del plazo pedido ('24' default | 'CI'). Plazo sin datos → 24hs."""
        with self._lock:
            snap = self._snap.get(settle)
            return dict(snap if snap else self._snap[SETTLE_24])


class HubMarketDataProvider(IMarketDataProvider):
    """`IMarketDataProvider` que sirve los snapshots desde el `ProviderHub` (el
    fetch async ya lo hizo `refresh_all`) y delega el histórico (CSV en disco /
    endpoint de OHLC) a un `Data912MarketDataProvider` sync.

    Esto cablea la ingesta async en el hot-path del refresh loop sin forzar
    async dentro del motor de pricing (que sigue corriendo sync vía to_thread):
    el use-case lee `fetch_snapshots` del snapshot ya materializado por el hub."""

    def __init__(self, hub: ProviderHub, history_provider: object = None,
                 settle: str = SETTLE_24):
        self._hub = hub
        self._settle = settle  # plazo de los precios live ('24' | 'CI')
        if history_provider is None:
            from core.infrastructure.repositories import Data912MarketDataProvider
            history_provider = Data912MarketDataProvider()
        self._hist = history_provider

    def fetch_snapshots(self, tickers: List[str]) -> Dict[str, MarketSnapshot]:
        snap = self._hub.snapshot(self._settle)
        out: Dict[str, MarketSnapshot] = {}
        today = date.today()
        for ticker in tickers:
            t = str(ticker).upper()
            # _CER es alias de display del lado CER de los duales (TXMJ8_CER → TXMJ8).
            market_t = t[:-4] if t.endswith("_CER") else t
            row = snap.get(market_t) or snap.get(t)
            if not row:
                continue
            out[ticker] = MarketSnapshot(
                instrument=None, price=float(row.c or 0.0), last_update=today,
                bid=row.px_bid, ask=row.px_ask, volume=row.v,
                operations=row.q_op, change_pct=row.pct_change,
            )
        return out

    def fetch_historical_prices(self, ticker: str, days: int):
        return self._hist.fetch_historical_prices(ticker, days)

    def fetch_stock_history(self, ticker: str):
        return self._hist.fetch_stock_history(ticker)
