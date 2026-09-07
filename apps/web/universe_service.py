"""Borde del job diario de novedades del universo (spec 2026-09-07 §2).

Lee el hub (snapshot ACUMULADO: a las 08:00 BYMA responde `data: []` y un fetch fresco
rechazaría la corrida todas las mañanas), escribe `byma_catalog` (solo agrega; `last_seen`
para lo visto) y `universe_novedades` (solo agrega; `nueva` → `cargada`), sella la corrida
en `schema_meta`, y NUNCA escribe `instruments`. Sync: corre en `to_thread` desde
`app._universe_loop` y desde «Refrescar ahora» (POST admin del ABM), una corrida por vez.

Las filas que este job agrega a `byma_catalog` alimentan, en el próximo arranque,
`_universe_groups`/`backfill_legs_from_universe` (app.py `_backfill_legs`): por eso
`ticker_pesos`/`moneda` siguen la convención del seed (ver `novedades.meta_de`).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Set

from sqlalchemy import func, select, update

from core.infrastructure.byma import novedades as nov
from core.infrastructure.byma.universe import _categoria, _loaded_ids
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import BymaCatalogORM, UniverseNovedadORM

logger = logging.getLogger(__name__)
_audit = logging.getLogger("monitor.audit")

META_ULTIMA_CORRIDA = "universe_ultima_corrida"   # 'YYYY-MM-DD' (hora AR)
META_ULTIMOS_VISTOS = "universe_ultimos_vistos"   # cuántos símbolos vio (guard relativo)
_MAX_FICHAS = 200   # fichas BYMA por corrida (una POST sync por símbolo); el resto se
                    # reintenta en corridas siguientes mientras la novedad siga pendiente
                    # sin ISIN

# Orden de prioridad del tope de fichas, por `security_type` de la fila: los títulos
# públicos y letras (GO) antes que las ON (CORP). Lo que no tiene hoja en el ABM
# (`novedades.CATEGORIAS_SIN_HOJA`) ni siquiera entra a la cola.
_RANK_FICHA = {"GO": 0, "CORP": 1}

# Una corrida por vez: el loop de las 08:00 y «Refrescar ahora» corren en hilos distintos
# y las dos leerían el mismo `catalogo` para después insertar el mismo PK.
_CORRIDA = threading.Lock()

FichaFn = Callable[[str], Optional[dict]]


@dataclass
class Resultado:
    vistos: int = 0
    altas_catalogo: int = 0
    nuevas: List[str] = field(default_factory=list)
    cargadas: List[str] = field(default_factory=list)
    pendientes: int = 0          # novedades en `nueva` al terminar (lo que muestra el badge)
    fichas: int = 0
    rechazo: Optional[str] = None

    def resumen(self) -> str:
        if self.rechazo:
            return "universo: corrida RECHAZADA (%s); %d pendiente(s)" % (
                self.rechazo, self.pendientes)
        return ("universo: %d vistos, +%d al catálogo, %d nueva(s), %d pasan a cargada, "
                "%d ficha(s), %d pendiente(s)" % (
                    self.vistos, self.altas_catalogo, len(self.nuevas), len(self.cargadas),
                    self.fichas, self.pendientes))


def ultima_corrida() -> Optional[str]:
    """'YYYY-MM-DD' de la última corrida sellada, o None."""
    return nov.leer_meta(META_ULTIMA_CORRIDA)


def _ficha_byma_factory() -> FichaFn:
    """Ficha técnica BYMA por símbolo (sync, `requests`, best-effort: None si no hay).
    Reusa los primitivos de `catalog_enrich`; una sesión para toda la corrida."""
    from core.infrastructure.byma.catalog_enrich import (
        _curate_ficha, _ficha_raw, _ficha_session,
    )
    session = _ficha_session()

    def _ficha(symbol: str) -> Optional[dict]:
        raw = _ficha_raw(session, symbol)
        if not raw:
            return None
        cur = _curate_ficha(raw)
        return {
            "isin": raw.get("codigoIsin") or None,
            "emisor": raw.get("emisor") or None,
            "denominacion": raw.get("denominacion") or None,
            "tipo_especie": cur.get("tipo_especie"),
            "vencimiento": cur.get("fecha_vencimiento"),
        }
    return _ficha


def _marcar_last_seen(s, symbols: Set[str], hoy_iso: str) -> None:
    syms = sorted(symbols)
    for i in range(0, len(syms), 500):   # SQLite limita las variables por statement
        s.execute(update(BymaCatalogORM)
                  .where(BymaCatalogORM.symbol.in_(syms[i:i + 500]))
                  .values(last_seen=hoy_iso))


def _rechazo(vistos: int, motivo: str) -> Resultado:
    logger.warning("universo: %s", motivo)
    return Resultado(vistos=vistos, rechazo=motivo, pendientes=nov.contar_nuevas())


def sincronizar_universo(hub, *, hoy: date, ficha_fn: Optional[FichaFn] = None,
                         max_fichas: int = _MAX_FICHAS) -> Resultado:
    """Una corrida. Devuelve qué pasó; con `rechazo` no se tocó nada ni se selló el día."""
    if not _CORRIDA.acquire(blocking=False):
        return _rechazo(0, "ya hay una corrida en curso")
    try:
        return _sincronizar(hub, hoy=hoy, ficha_fn=ficha_fn, max_fichas=max_fichas)
    finally:
        _CORRIDA.release()


def _sincronizar(hub, *, hoy: date, ficha_fn: Optional[FichaFn], max_fichas: int) -> Resultado:
    snapshot = hub.snapshot()
    sources = hub.sources()
    # `freshness()` lista lo que trajo la fuente ACTIVA, sea cual sea: sólo es «byma» si
    # la activa es BYMA (open/realtime); con Data912 activa todo vino de Data912.
    activa = str(getattr(hub, "active_mode", "") or "")
    frescos = set(hub.freshness()) if activa.startswith("byma") else set()
    vistos: Dict[str, str] = {sym: sources.get(sym, "") for sym in snapshot}

    init_db()
    ref = nov.leer_meta(META_ULTIMOS_VISTOS)
    with SessionLocal() as s:
        catalogo = {r[0].upper() for r in s.execute(select(BymaCatalogORM.symbol)).all() if r[0]}
        registradas, pendientes = nov.simbolos_registrados(s)
    if not catalogo:
        # Sin línea de base el diff marcaría TODO el feed como novedad y dejaría la
        # siembra del CSV (`app._seed_byma_universe`, sólo si vacía) en no-op para siempre.
        return _rechazo(len(vistos), "byma_catalog vacío: falta la siembra del universo "
                                     "(sin línea de base para el diff)")
    _isins, cargados = _loaded_ids()

    diff = nov.clasificar(vistos, frescos, catalogo, registradas, pendientes, cargados,
                          ref_vistos=int(ref) if ref and ref.isdigit() else None)
    if diff.rechazo:
        return _rechazo(diff.vistos, diff.rechazo)
    res = Resultado(vistos=diff.vistos)

    # Reintentos: novedades pendientes cuya fila de `byma_catalog` sigue sin ISIN — la
    # corrida en que se altearon puede haberse quedado sin ficha (tope o falla) y, una vez
    # en el catálogo, `diff.altas_catalogo` no las vuelve a traer. Acotado a PENDIENTES
    # sin ISIN a propósito: no hay que gastar el tope en los ~664 símbolos del CSV seed
    # que nunca tuvieron ISIN (no son novedad). Y acotado, además, a lo CARGABLE (BYMA no
    # tiene ficha de acciones/cedears: esas pendientes se llevaban un lugar del tope en
    # cada corrida) y a lo que tiene fila en el catálogo (join interno: sin fila no hay
    # dónde escribir el resultado de la ficha).
    altas_symbols = {fila["symbol"] for fila in diff.altas_catalogo}
    sin_hoja = sorted(nov.CATEGORIAS_SIN_HOJA)
    with SessionLocal() as s:
        candidatos_pendientes = s.execute(
            select(UniverseNovedadORM.symbol)
            .join(BymaCatalogORM, BymaCatalogORM.symbol == UniverseNovedadORM.symbol)
            .where(UniverseNovedadORM.estado == "nueva", BymaCatalogORM.isin.is_(None),
                   func.coalesce(BymaCatalogORM.categoria, "").notin_(sin_hoja))
        ).scalars().all()
    reintentos = sorted(sym for sym in candidatos_pendientes if sym not in altas_symbols)

    # Ficha técnica para símbolos nuevos + pendientes sin ISIN. Red, FUERA de la
    # transacción. El tope es el recurso escaso de la corrida: se gasta sólo donde el ABM
    # tiene hoja (para una acción/cedear la ficha responde `data: []`) y primero en
    # títulos públicos/letras, que si no quedaban detrás de cualquier símbolo alfabético.
    ficha_fn = ficha_fn or _ficha_byma_factory()
    cargables = sorted(
        (f for f in diff.altas_catalogo if f["categoria"] not in nov.CATEGORIAS_SIN_HOJA),
        key=lambda f: (_RANK_FICHA.get(f["security_type"] or "", 2), f["symbol"]))
    todos_candidatos = [fila["symbol"] for fila in cargables] + reintentos
    fichas: Dict[str, dict] = {}
    for symbol in todos_candidatos[:max_fichas]:
        try:
            f = ficha_fn(symbol)
        except Exception as e:  # noqa: BLE001 — la ficha es best-effort
            logger.debug("ficha %s falló: %s", symbol, e)
            f = None
        if f:
            fichas[symbol] = f
    if len(todos_candidatos) > max_fichas:
        logger.info("universo: %d símbolos sin ficha esta corrida (tope %d); se reintentan "
                    "mientras sigan pendientes, cargables y sin ISIN",
                    len(todos_candidatos) - max_fichas, max_fichas)

    hoy_iso = hoy.isoformat()
    ahora = datetime.now().isoformat(timespec="seconds")
    with SessionLocal.begin() as s:
        for fila in diff.altas_catalogo:
            f = fichas.get(fila["symbol"]) or {}
            campos = {k: v for k, v in fila.items() if k != "source"}
            campos["categoria"] = _categoria(f.get("tipo_especie") or "",
                                             campos["security_type"] or "",
                                             campos["panel"] or "")
            fila["categoria"] = campos["categoria"]   # la novedad guarda la misma categoría
            s.add(BymaCatalogORM(**campos, isin=f.get("isin"), emisor=f.get("emisor"),
                                 denominacion=f.get("denominacion"),
                                 vencimiento=f.get("vencimiento"),
                                 last_seen=hoy_iso, updated_at=ahora))
        for symbol in reintentos:
            f = fichas.get(symbol)
            if not f:
                continue
            row = s.get(BymaCatalogORM, symbol)   # existe: el join de arriba es interno
            # Sólo rellena huecos: nunca pisa un valor ya cargado (misma regla que
            # `catalog_enrich.enrich_isin_from_byma` — la fila viva manda).
            if row.isin is None and f.get("isin"):
                row.isin = f["isin"]
            if row.emisor is None and f.get("emisor"):
                row.emisor = f["emisor"]
            if row.denominacion is None and f.get("denominacion"):
                row.denominacion = f["denominacion"]
            if row.vencimiento is None and f.get("vencimiento"):
                row.vencimiento = f["vencimiento"]
            if f.get("tipo_especie"):
                nueva_categoria = _categoria(f["tipo_especie"], row.security_type or "",
                                             row.panel or "")
                if nueva_categoria != row.categoria:
                    row.categoria = nueva_categoria
                    nov_row = s.get(UniverseNovedadORM, symbol)
                    if nov_row is not None:
                        nov_row.categoria = nueva_categoria
        _marcar_last_seen(s, {sym for sym in vistos if sym in catalogo}, hoy_iso)
        res.nuevas = nov.registrar_nuevas_en(s, diff.nuevas, hoy=hoy)
        res.cargadas = nov.marcar_cargadas_en(s, diff.cargadas)
        nov.escribir_meta_en(s, META_ULTIMA_CORRIDA, hoy_iso)
        nov.escribir_meta_en(s, META_ULTIMOS_VISTOS, diff.vistos)
    res.altas_catalogo = len(diff.altas_catalogo)
    res.fichas = len(fichas)
    for sym in res.nuevas:
        # Deja rastro en journald: es lo que va a mirar el operador cuando pregunte
        # "¿desde cuándo está esto?".
        _audit.info("universo action=novedad symbol=%s", sym, extra={"console": True})
    res.pendientes = nov.contar_nuevas()
    return res
