"""AppState: snapshot vivo del último reporte (reemplaza la `class Snapshot` del
http.server). Async-safe; el refresh loop del lifespan lo actualiza, los routers
lo leen vía Depends(get_state)."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from core.domain.models import InstrumentMetrics

# Formato del mensaje de caída ("loop <name> cayó (<motivo>) — reiniciando"). El
# camino VIVO es estructurado (`record_loop_crash(name, reason)`, que es lo que llama
# `_on_crash` de app.py): este regex quedó SOLO como back-compat para un `record_error()`
# que traiga el string ya armado. No colgar contratos nuevos de él — parsear el wording
# rompía en silencio al cambiar una palabra, y `record_error` trunca a 300 chars ANTES
# de aplicarlo, así que un motivo largo dejaba el ')' fuera del corte.
_LOOP_CRASH_RE = re.compile(r"^loop (\S+) cayó \((.*)\)")

# Loops cuya caída es CRÍTICA: apagan el semáforo (badge rojo "sin datos" +
# /api/health degradado) porque sin ellos la app no tiene qué mostrar. Sólo el
# refresh loop produce el snapshot que sirven los paneles; ratings/bei/options/
# price_history alimentan funciones LATERALES y su caída es una degradación
# PARCIAL: queda en `loop_crashes`/`degraded_loops` (y en el log), pero no pinta de
# rojo un panel de precios que está perfecto. Antes, con la retención de 300s, que
# se cayera el scraper de calificaciones dejaba el header en "sin datos" 5 minutos.
_CRITICAL_LOOPS = frozenset({"refresh"})

# Cuánto retiene el badge (`last_error`) la marca de una caída CRÍTICA pese a los
# refresh exitosos que vengan después: el badge del header poll-ea cada 15s, así que
# con la limpieza incondicional de `update()` (cada 5s) prácticamente no se veía.
# También es la ventana de "caída reciente" que reporta `degraded_loops()`.
_CRASH_STICKY_S = 300.0
# Ventana del registro por loop que expone `status()` (diagnóstico, no badge).
_CRASH_HISTORY_S = 24 * 3600.0


# Salud del CATÁLOGO (canal propio, ver `set_catalog_health`). No es una métrica de
# frescura: describe QUÉ hay cargado, no cuán reciente es. Vive separada de `_error`
# porque un ciclo de refresh exitoso no dice absolutamente nada del catálogo —
# `update()` limpia el error del ciclo y borraría la señal en 5 segundos.
_EMPTY_CATALOG_HEALTH: Dict[str, Any] = {
    "instruments": 0,      # especies cargadas (patas ARS/MEP/CABLE incluidas)
    "orphans": [],         # tickers con tipo fuera de instrument_groups (invisibles)
    "defaulted": [],       # tickers con el tipo ASUMIDO de un default ambiguo
    "seed_error": None,    # motivo del fallo de la siembra de bootstrap (o None)
    "at": None,            # cuándo se publicó
}


def _loop_crash_of(msg: Optional[str]) -> Optional[tuple]:
    """(loop, motivo) si `msg` es una caída reportada por el supervisor; si no None."""
    if not msg:
        return None
    m = _LOOP_CRASH_RE.match(msg)
    return (m.group(1), m.group(2)) if m else None


class AppState:
    def __init__(self, crash_sticky_s: float = _CRASH_STICKY_S) -> None:
        self._metrics: List[InstrumentMetrics] = []
        self._by_ticker: Dict[str, InstrumentMetrics] = {}
        self._last_refresh: Optional[datetime] = None
        # Observabilidad (O1): último error del refresh loop, como UN par (msg, ts).
        # Un solo atributo = asignación atómica bajo el GIL → los lectores sync
        # (health/badge corren en el threadpool de Starlette) nunca ven el mensaje
        # sin su timestamp. La app sigue sirviendo el último snapshot bueno.
        # (msg, at, crash_loop | None). El TERCER elemento dice si la marca vino
        # de la caída de un loop crítico: de ahí sale la retención de `_keep_error`.
        # Antes eso se re-deducía parseando `msg` con `_LOOP_CRASH_RE`, y como el
        # mensaje se trunca a 300 chars, un motivo largo (>281) dejaba el ')' fuera
        # del corte → el badge de una caída del REFRESH loop se borraba al ciclo
        # siguiente (5s), que es exactamente el bug que la retención venía a tapar.
        self._error: Optional[tuple] = None
        # Canal PROPIO de las caídas de loops supervisados: {loop: (motivo, at)}.
        # `update()` NO lo toca — el éxito de un loop no dice nada del que se cayó.
        self._loop_crashes: Dict[str, tuple] = {}
        self._crash_sticky_s = float(crash_sticky_s)
        self._bei: Optional[dict] = None  # tablas crudas de compute_bei_tables
        self._options: list = []          # list[OptionItem] del último refresh (vacío hasta que arme)
        self._options_by_ticker: Dict[str, object] = {}
        # Fuente de datos activa (mode/label/delayed) — la setea el lifespan y el
        # endpoint de switch; el header la muestra.
        self._data_source: Dict[str, object] = {"mode": "", "label": "", "delayed": False}
        # Salud del catálogo publicada por el arranque (CatalogRepository.type_health
        # + el fallo de siembra). Canal PROPIO: `update()` no lo toca.
        self._catalog: Dict[str, Any] = dict(_EMPTY_CATALOG_HEALTH)
        self._lock = asyncio.Lock()
        # Notificación para SSE (§7.4): cada update incrementa _revision y
        # despierta a los suscriptores de /stream (push event-driven vs polling).
        self._revision = 0
        self._cond = asyncio.Condition()

    async def update(self, metrics: List[InstrumentMetrics]) -> None:
        by_ticker = {
            m.snapshot.instrument.ticker: m
            for m in metrics
            if m.snapshot and m.snapshot.instrument
        }
        async with self._lock:
            self._metrics = metrics
            self._by_ticker = by_ticker
            self._last_refresh = datetime.now()
            # Un refresh exitoso limpia el error del PROPIO ciclo, pero no la marca
            # de una caída CRÍTICA reciente: el badge poll-ea cada 15s y el refresh
            # corre cada 5s, así que sin retención la caída no se veía nunca. Las
            # caídas de loops laterales ni siquiera llegan acá (viven en
            # `_loop_crashes`, que `update()` no toca).
            self._error = self._keep_error(self._error, self._last_refresh)
        await self._notify()

    def _keep_error(self, error: Optional[tuple], now: datetime) -> Optional[tuple]:
        """Qué sobrevive a un ciclo de refresh exitoso: nada, salvo la marca de una
        caída CRÍTICA todavía dentro de su ventana de retención."""
        if error is None or error[2] is None:      # error de ciclo, no una caída
            return None
        return error if (now - error[1]).total_seconds() < self._crash_sticky_s else None

    async def record_error(self, msg: str) -> None:
        """Registra un fallo de ciclo de refresh. NO borra el último snapshot bueno
        (se sigue sirviendo data stale) y NO notifica SSE: despertar a los paneles
        para re-fetchear data que no cambió era un storm de requests durante outages
        (~12/min × panel); el badge del header se entera por su propio polling.

        Back-compat: si el mensaje YA viene con el formato de caída del supervisor se
        re-encamina al canal estructurado (el camino vivo es `record_loop_crash`)."""
        msg = str(msg)[:300]
        crash = _loop_crash_of(msg)
        if crash is not None:
            await self.record_loop_crash(*crash)
            return
        self._error = (msg, datetime.now(), None)        # asignación atómica de la tupla

    async def record_loop_crash(self, name: str, reason: str) -> None:
        """Caída de un loop supervisado (canal explícito de `supervise(on_crash=...)`).

        El nombre del loop llega ESTRUCTURADO (no parseando el mensaje): de ahí sale
        la severidad. Un loop crítico además marca el badge/health; el resto queda
        sólo en el registro por loop, que `update()` nunca toca."""
        name, reason, at = str(name), str(reason)[:300], datetime.now()
        # Copy-on-write: `status()` corre en el threadpool de Starlette y acá escribe
        # el event loop → publicar un dict NUEVO (asignación atómica) evita que el
        # lector itere el dict mientras le insertan una clave.
        crashes = dict(self._loop_crashes)
        crashes[name] = (reason, at)
        self._loop_crashes = crashes
        if name in _CRITICAL_LOOPS:
            self._error = (f"loop {name} cayó ({reason}) — reiniciando"[:300], at, name)

    def loop_crashes(self, now: Optional[datetime] = None) -> List[dict]:
        """Caídas de loops de las últimas 24hs, más reciente primero."""
        now = now or datetime.now()
        cutoff = now - timedelta(seconds=_CRASH_HISTORY_S)
        crashes = self._loop_crashes        # una sola lectura → snapshot coherente
        out = [{"loop": n, "reason": reason, "at": at.isoformat()}
               for n, (reason, at) in crashes.items() if at >= cutoff]
        out.sort(key=lambda c: c["at"], reverse=True)
        return out

    def degraded_loops(self, now: Optional[datetime] = None) -> List[str]:
        """Loops con una caída RECIENTE (dentro de la ventana de retención).

        Señal de degradación PARCIAL, separada del semáforo de los precios: sirve
        para que health/ops vean qué función lateral se cayó sin declarar 'sin datos'
        un panel que está refrescando bien. Sólo nombres — el motivo es el string
        crudo de una excepción y `/api/health` es público."""
        now = now or datetime.now()
        crashes = self._loop_crashes        # una sola lectura → snapshot coherente
        return sorted(n for n, (_reason, at) in crashes.items()
                      if (now - at).total_seconds() < self._crash_sticky_s)

    def set_catalog_health(self, *, instruments: int, orphans: Iterable = (),
                           defaulted: Iterable = (),
                           seed_error: Optional[str] = None) -> None:
        """Publica la salud del catálogo (un solo escritor: el arranque / el reconcile).

        Es el consumidor que le faltaba a `CatalogRepository.type_health`: sin esto el
        reporte de tipos existía y NADIE lo leía (sus únicos lectores eran los tests),
        así que un bono con tipo huérfano —cargado, con cashflows, con precio, pero
        invisible en TODOS los paneles— volvía a serlo en silencio.

        Copy-on-write: se publica un dict NUEVO (asignación atómica bajo el GIL), que
        es lo que hace seguro leerlo desde el threadpool de Starlette mientras el event
        loop escribe."""
        self._catalog = {
            "instruments": int(instruments),
            "orphans": [str(t) for t in orphans],
            "defaulted": [str(t) for t in defaulted],
            "seed_error": str(seed_error)[:300] if seed_error else None,
            "at": datetime.now(),
        }

    def catalog_health(self) -> Dict[str, Any]:
        """Reporte COMPLETO (uso interno: incluye tickers y el motivo del fallo)."""
        return dict(self._catalog)         # una sola lectura → snapshot coherente

    def catalog_status(self) -> Dict[str, Any]:
        """Vista PÚBLICA del reporte, para `/api/health`: cuenta los baldes y dice si
        la siembra falló, SIN el string crudo de la excepción (arrastra paths del
        servidor) ni el inventario de tickers."""
        c = self._catalog
        return {"instruments": c["instruments"], "orphans": len(c["orphans"]),
                "defaulted": len(c["defaulted"]),
                "seed_failed": c["seed_error"] is not None}

    def _catalog_error(self) -> Optional[tuple]:
        """El fallo de SIEMBRA como error de badge, en la forma de `self._error`.

        Es STICKY a propósito: mientras el catálogo esté vacío porque la semilla no se
        pudo leer, un refresh 'exitoso' de 0 instrumentos no arregla nada y no puede
        pintar el badge de verde. Cede la prioridad a un error de ciclo (más fresco y
        más específico)."""
        c = self._catalog
        err = c.get("seed_error")
        if not err:
            return None
        return (f"catálogo vacío: la siembra desde el Excel falló ({err})"[:300],
                c.get("at") or datetime.now(), None)

    def status(self, *, stale_after_s: Optional[float] = None,
               now: Optional[datetime] = None) -> dict:
        """Estado de frescura para health/header: edad del último refresh, si está
        stale (más viejo que `stale_after_s` o nunca refrescado) y el último error.

        `stale_after_s` default = 6 ciclos de refresh (tolera blips transitorios del
        breaker sin alarmar) — centralizado acá para que health y badge no dupliquen
        el umbral. `now` se inyecta en tests."""
        if stale_after_s is None:
            from config.settings import settings
            stale_after_s = settings.refresh_sec * 6
        now = now or datetime.now()
        # Una sola lectura → par consistente. El fallo de siembra del catálogo entra
        # como fallback: no lo borra un ciclo de refresh (ver `_catalog_error`).
        error = self._error or self._catalog_error()
        age = (now - self._last_refresh).total_seconds() if self._last_refresh else None
        is_stale = age is None or age > stale_after_s
        return {
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
            "age_seconds": age,
            "is_stale": is_stale,
            "last_error": error[0] if error else None,
            "last_error_at": error[1].isoformat() if error else None,
            "loop_crashes": self.loop_crashes(now),
            # Degradación parcial (loops laterales): NO entra en `ok` — el semáforo
            # del header y el status de /api/health hablan de los PRECIOS.
            "degraded_loops": self.degraded_loops(now),
            # Salud del CATÁLOGO (qué hay cargado), separada de la frescura. No entra
            # en `ok`: un tipo huérfano es una condición CRÓNICA (hoy hay decenas de
            # filas así) y degradar el semáforo por eso lo dejaría rojo para siempre,
            # que es la forma más rápida de que nadie lo mire. Lo que sí entra —vía
            # `_catalog_error`— es el catálogo VACÍO por una siembra fallida: ahí
            # literalmente no hay datos que servir.
            "catalog": self.catalog_status(),
            "ok": (not is_stale) and error is None,
        }

    @property
    def last_error(self) -> Optional[str]:
        error = self._error
        return error[0] if error else None

    async def _notify(self) -> None:
        async with self._cond:
            self._revision += 1
            self._cond.notify_all()

    @property
    def revision(self) -> int:
        return self._revision

    async def wait_for_change(self, last_seen: int) -> int:
        """Bloquea hasta que la revisión difiera de `last_seen`; devuelve la nueva."""
        async with self._cond:
            await self._cond.wait_for(lambda: self._revision != last_seen)
            return self._revision

    def metrics(self) -> List[InstrumentMetrics]:
        return self._metrics

    def by_ticker(self, ticker: str) -> Optional[InstrumentMetrics]:
        return self._by_ticker.get(ticker.upper())

    def price_of(self, ticker: str) -> Optional[float]:
        m = self._by_ticker.get(ticker.upper())
        return m.snapshot.price if m and m.snapshot else None

    @property
    def last_refresh(self) -> Optional[datetime]:
        return self._last_refresh

    def set_bei(self, tables: Optional[dict]) -> None:
        self._bei = tables  # un solo escritor (el BEI loop); asignación atómica

    def bei_tables(self) -> Optional[dict]:
        return self._bei

    def set_data_source(self, mode: str, label: str, delayed: bool) -> None:
        """Setea la fuente de datos activa (un solo escritor a la vez)."""
        self._data_source = {"mode": mode, "label": label, "delayed": bool(delayed)}

    def data_source(self) -> Dict[str, object]:
        return dict(self._data_source)

    def set_options(self, items: list) -> None:
        """Setea la chain enriquecida de opciones (escrita por el refresh loop).

        Construye el índice ANTES de publicar las referencias: los lectores
        (option()/options(), que corren en el thread pool de FastAPI) ven siempre
        un dict completo y coherente, nunca uno a medio llenar."""
        items = items or []
        by_ticker = {it.ticker: it for it in items}
        self._options = items
        self._options_by_ticker = by_ticker

    def options(self) -> list:
        return self._options

    def option(self, ticker: str):
        """Devuelve el OptionItem del ticker dado o None."""
        return self._options_by_ticker.get(ticker.upper())
