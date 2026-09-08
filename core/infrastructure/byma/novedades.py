"""Novedades del universo: qué especies aparecieron en los feeds que el universo conocido
no tenía, y el triage de cada una (spec docs/superpowers/specs/2026-09-07-novedades-universo-design.md).

REGLAS DURAS (mismo espíritu que `letras_sync`):

1. **Detección ≠ alta.** Acá no se escribe `instruments` jamás: el alta la dispara el
   operador desde el ABM con el form prefillado.
2. **Nunca se borra.** Ni una fila de `byma_catalog` (el job sólo agrega/actualiza) ni una
   de `universe_novedades` (`descartada` es un ESTADO, no un delete).
3. **Una lectura anémica no decide nada.** Si el hub trae muchos menos símbolos que la
   corrida anterior (server recién arrancado pre-market, breaker abierto), la corrida se
   rechaza entera y se reintenta más tarde.
4. **Visto = cotizó, y una variante no es una especie.** El hub conserva en el snapshot el
   maestro entero de BYMA con precio 0 (especies ilíquidas o vencidas hace años: un 0 no
   es dato, CLAUDE.md) — eso lo filtra el borde (`universe_service`). Acá, el espejo del
   segmento bilateral (`.SB`) y las patas de plazo especial (X/Y/Z) ni entran al catálogo
   ni son novedad, y las patas D/C de una especie se cuentan UNA vez (`ticker_pesos`).
   Sin esto la primera corrida en prod (2026-09-07) registró 4155 «novedades» (AA17,
   AL02H, AO29X…) y dejó 17 variantes con el ISIN de un bono cargado y `cotiza=1`, que
   `backfill_legs_from_universe` habría escrito en `instruments` en el próximo arranque.

La lógica de decisión (`clasificar`, `proximo_despertar`) es PURA y recibe el reloj y las
lecturas como parámetros; el acceso a la base vive abajo, en funciones chicas que reciben
la sesión (`*_en(s, ...)`) o abren la suya.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import func, inspect, select

from core.domain.currency import ccy_from_suffix
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal, get_engine
from core.infrastructure.db.models import BymaCatalogORM, UniverseNovedadORM

logger = logging.getLogger(__name__)

# Guard de la lectura entera (criterio hermano de ratings/letras): por debajo del piso
# ABSOLUTO el hub todavía no vio la rueda (arranque pre-market); por debajo del piso
# RELATIVO a la corrida anterior es un corte parcial (breaker abierto, panel vacío).
_MINIMO_VISTOS = 50
_PISO_RELATIVO = 0.6

# Hora de la corrida diaria (AR): antes de la rueda, con el hub todavía con la de ayer.
_HORA_CORRIDA = 8

# bucket del hub (mismo vocabulario en BYMA open y Data912: `ProviderHub.sources()`) →
# cómo se ve la especie en el universo. `security_type`/`panel` espejan los valores del
# seed CSV para que `universe._categoria` y el agrupado del ABM traten igual a una fila
# del job que a una del CSV. Un bucket desconocido cae en "Otros": nunca se inventa.
_BUCKET_META: Dict[str, Dict[str, Optional[str]]] = {
    "notes":   {"security_type": "GO",   "panel": "Letras",             "ins_type": "BOND",   "categoria": "Títulos Públicos"},
    "bonds":   {"security_type": "GO",   "panel": "Titulos Publicos",   "ins_type": "BOND",   "categoria": "Títulos Públicos"},
    "corp":    {"security_type": "CORP", "panel": "Oblig. Negociables", "ins_type": "BOND",   "categoria": "Obligaciones Negociables"},
    "stocks":  {"security_type": "CS",   "panel": "Acciones General",   "ins_type": "EQUITY", "categoria": "Acciones"},
    "cedears": {"security_type": "CD",   "panel": "CEDEARs",            "ins_type": "EQUITY", "categoria": "Cedears"},
}
_META_OTROS: Dict[str, Optional[str]] = {"security_type": None, "panel": None,
                                        "ins_type": None, "categoria": "Otros"}

# Sufijo → vocabulario de `byma_catalog.moneda` (el seed usa ARS/MEP/cable, no CABLE).
_MONEDA_CATALOGO = {"ARS": "ARS", "MEP": "MEP", "CABLE": "cable"}

# Categorías que NO tienen hoja en el ABM: la pestaña Novedades sólo ofrece Descartar
# (las acciones ya se registran solas al arranque; cedears/índices no se precian).
CATEGORIAS_SIN_HOJA = frozenset({
    "Acciones", "Cedears", "Índices", "Totales", "Futuros", "Acciones Internacionales",
    "Otros",
})

# Categorías que se dan de ALTA SOLAS (decisión de David, 2026-09-07): una acción o un
# CEDEAR no necesita términos ni flujos para sus métricas —es el mismo instrumento con
# sólo el ticker (`instruments_abm.register_stocks`)—, así que no hay nada que decidir y
# no son novedad. Si el alta automática falla, la especie sí queda como novedad.
CATEGORIAS_AUTO_ALTA = frozenset({"Acciones", "Cedears"})

# Variantes de un símbolo que NO son una especie nueva (regla 4): el espejo del segmento
# bilateral (`.SB`, SENEBI) y las patas de otro ÁMBITO de negociación — sufijo X/Y/Z
# sobre la raíz de 4 letras del símbolo primario (AL30 → AL30X/Y/Z, TY30P → TY30X/Y/Z,
# CRES → CRESX), que el seed CSV trae como `especial`/`cotiza=0`. Mismo activo (mismo
# ISIN): el ticker sólo cambia por moneda (D/C) y por ámbito (X/Y/Z). La raíz se exige
# compartida con OTRO símbolo visto para no confundir un ticker real de 5 letras que
# termine en X/Y/Z con una variante (NFLX tiene 4: nunca entra).
_SUFIJO_BILATERAL = ".SB"
_SUFIJOS_AMBITO = "XYZ"

# Orden de las patas de una especie al elegir cuál registrar como novedad: la pesos si
# cotiza; si no, la MEP; la cable al final.
_RANK_MONEDA = {"ARS": 0, "MEP": 1, "cable": 2}


def _raices(symbols: Iterable[str]) -> Dict[str, int]:
    """Cuántos símbolos vistos comparten cada raíz de 4 letras (para `es_variante`)."""
    out: Dict[str, int] = {}
    for s in symbols:
        s = _norm(s)
        if len(s) >= 4 and not s.endswith(_SUFIJO_BILATERAL):
            out[s[:4]] = out.get(s[:4], 0) + 1
    return out


def es_variante(symbol: str, bucket: str, raices: Dict[str, int]) -> bool:
    """True si el símbolo es el espejo `.SB` o una pata de otro ámbito (X/Y/Z) de una
    especie que también se vio, en cualquier bucket. `raices` sale de `_raices(vistos)`;
    `bucket` queda en la firma por si un feed nuevo exige distinguirlo."""
    del bucket
    sym = _norm(symbol)
    if sym.endswith(_SUFIJO_BILATERAL):
        return True
    if len(sym) == 5 and sym[-1] in _SUFIJOS_AMBITO:
        return raices.get(sym[:4], 0) > 1
    return False


def meta_de(symbol: str, bucket: str) -> dict:
    """Fila de `byma_catalog` para un símbolo que vino de los feeds: metadata por bucket
    + moneda por sufijo (`core.domain.currency`, única fuente) + ticker base con la MISMA
    convención del seed CSV — importa porque `search_byma_grouped`/`count_unloaded`
    agrupan por `isin or ticker_pesos or symbol` y `backfill_legs_from_universe` (cada
    arranque) arma las patas por `ticker_pesos`: soberanos/letras `AL30D→AL30`; ON
    `AEC2D→AEC2O` (la pata pesos completa, con su `O`); acciones/cedears `ALUAD→ALUA`."""
    sym = (symbol or "").upper().strip()
    m = _BUCKET_META.get(bucket or "", _META_OTROS)
    ccy = ccy_from_suffix(sym)
    base = sym[:-1] if ccy in ("MEP", "CABLE") and len(sym) > 1 else sym
    if bucket == "corp" and base != sym:
        base += "O"
    return {
        "symbol": sym,
        "ticker_pesos": base,
        "moneda": _MONEDA_CATALOGO[ccy],
        "categoria": m["categoria"],
        "security_type": m["security_type"],
        "panel": m["panel"],
        "ins_type": m["ins_type"],
        "clase_liquidacion": "primary",
        "cotiza": 1,
    }


@dataclass
class Diff:
    """Qué haría la corrida. Nada de esto se ejecutó todavía."""

    altas_catalogo: List[dict] = field(default_factory=list)  # vistos que byma_catalog no tenía
    nuevas: List[dict] = field(default_factory=list)          # ⊆ altas_catalogo: a decidir
    cargadas: List[str] = field(default_factory=list)         # pendientes ya en instruments
    vistos: int = 0
    rechazo: Optional[str] = None


def _guard(n_vistos: int, ref_vistos: Optional[int]) -> Optional[str]:
    """Motivo para descartar la lectura entera, o None si es usable. Mira el TOTAL
    mergeado del hub (BYMA ∪ floor Data912), no una fuente en particular: el hub es
    stale-safe y no expone «qué fuente falló»."""
    if n_vistos == 0:
        return "el hub no tiene símbolos (¿server recién arrancado antes de la rueda?)"
    if n_vistos < _MINIMO_VISTOS:
        return "lectura anémica: %d símbolos (< %d)" % (n_vistos, _MINIMO_VISTOS)
    if ref_vistos and n_vistos < _PISO_RELATIVO * ref_vistos:
        return "corte parcial: %d símbolos < %d%% de los %d de la corrida anterior" % (
            n_vistos, int(_PISO_RELATIVO * 100), ref_vistos)
    return None


def clasificar(vistos: Dict[str, str], listados_byma: Set[str], catalogo: Set[str],
               registradas: Set[str], pendientes: Set[str], cargados: Set[str], *,
               ref_vistos: Optional[int] = None) -> Diff:
    """Diff puro de una corrida. No muta lo que recibe.

    `vistos`: {symbol: bucket} del hub (snapshot ∩ sources). `listados_byma`: símbolos que
    la fuente activa listó (`hub.freshness()`) SI la activa es BYMA; el resto vino del
    floor Data912. `catalogo`: símbolos de `byma_catalog` ANTES del upsert. `registradas`:
    símbolos ya en `universe_novedades` (cualquier estado) MÁS su especie
    (`simbolos_registrados`); `pendientes`: los que siguen `nueva`. `cargados`: tickers
    de `instruments` (primario + patas). `ref_vistos`: cuántos símbolos vio la corrida
    anterior (guard relativo).

    Regla 4: `.SB` y X/Y/Z (`es_variante`) ni entran al catálogo ni son novedad, y se
    registra UNA novedad por especie (`ticker_pesos`, la pata pesos primero): las patas
    D/C nuevas de una especie cargada o ya registrada no son otra novedad.
    """
    diff = Diff(vistos=len(vistos))
    diff.rechazo = _guard(len(vistos), ref_vistos)
    if diff.rechazo:
        return diff
    raices = _raices(vistos)
    filas: List[dict] = []
    for sym, bucket in vistos.items():
        sym = _norm(sym)
        if not sym or sym in catalogo or es_variante(sym, bucket, raices):
            continue
        fila = meta_de(sym, bucket)
        fila["source"] = "byma" if sym in listados_byma else "data912"
        filas.append(fila)
    filas.sort(key=lambda f: (f["ticker_pesos"], _RANK_MONEDA.get(f["moneda"] or "", 3),
                              f["symbol"]))
    emitidas: Set[str] = set()
    for fila in filas:
        diff.altas_catalogo.append(fila)
        sym, base = fila["symbol"], _norm(fila["ticker_pesos"])
        if sym in cargados or base in cargados or sym in registradas or base in registradas:
            continue
        if base in emitidas:
            continue
        emitidas.add(base)
        diff.nuevas.append(fila)
    diff.cargadas = sorted(p for p in pendientes if p in cargados)
    return diff


def proximo_despertar(now: datetime, *, hecha_hoy: bool) -> float:
    """Segundos hasta la próxima corrida. `now` es aware en hora AR (lo inyecta el loop;
    los tests lo fijan). Hecha hoy → mañana a las 08:00; antes de las 08:00 → hoy a las
    08:00; después, sin hacer → ahora (0)."""
    objetivo = now.replace(hour=_HORA_CORRIDA, minute=0, second=0, microsecond=0)
    if hecha_hoy:
        objetivo += timedelta(days=1)
    elif now >= objetivo:
        return 0.0
    return max(0.0, (objetivo - now).total_seconds())


# ── store: `universe_novedades` + claves propias en `schema_meta` ──────────────
def _ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm(symbol) -> str:
    return str(symbol or "").upper().strip()


def registrar_nuevas_en(s, nuevas: Iterable[dict], *, hoy) -> List[str]:
    """Inserta como `nueva` las que NO existen. Una fila existente (en cualquier estado)
    se respeta: el operador ya decidió, o el job ya la vio."""
    out: List[str] = []
    for f in nuevas:
        sym = _norm(f.get("symbol"))
        if not sym or s.get(UniverseNovedadORM, sym) is not None:
            continue
        s.add(UniverseNovedadORM(symbol=sym, first_seen=hoy.isoformat(),
                                 source=f.get("source") or "data912",
                                 categoria=f.get("categoria"), estado="nueva",
                                 updated_at=_ahora()))
        out.append(sym)
    return sorted(out)


def marcar_cargadas_en(s, symbols: Iterable[str]) -> List[str]:
    """`nueva` → `cargada` para los símbolos dados (sólo las pendientes)."""
    syms = {_norm(t) for t in symbols if t}
    if not syms:
        return []
    filas = s.execute(select(UniverseNovedadORM).where(
        UniverseNovedadORM.symbol.in_(sorted(syms)),
        UniverseNovedadORM.estado == "nueva")).scalars().all()
    for f in filas:
        f.estado = "cargada"
        f.updated_at = _ahora()
    return sorted(f.symbol for f in filas)


def marcar_cargadas(symbols: Iterable[str]) -> List[str]:
    init_db()
    with SessionLocal.begin() as s:
        return marcar_cargadas_en(s, symbols)


def _cambiar_estado(symbol: str, desde: str, hacia: str) -> bool:
    init_db()
    with SessionLocal.begin() as s:
        f = s.get(UniverseNovedadORM, _norm(symbol))
        if f is None or f.estado != desde:
            return False
        f.estado = hacia
        f.updated_at = _ahora()
        return True


def descartar(symbol: str) -> bool:
    """`nueva` → `descartada` (reversible con `restaurar`). Nunca borra."""
    return _cambiar_estado(symbol, "nueva", "descartada")


def restaurar(symbol: str) -> bool:
    return _cambiar_estado(symbol, "descartada", "nueva")


def descartar_grupo(categoria: str) -> int:
    """`nueva` → `descartada` para TODAS las pendientes de una categoría (la que muestra
    el listado: la de `byma_catalog` si hay fila, si no la guardada al detectarse).
    Reversible una por una con `restaurar`; nunca borra. Devuelve cuántas cambió."""
    init_db()
    syms = sorted(f["symbol"] for f in listar("nueva") if f["categoria"] == categoria)
    if not syms:
        return 0
    n = 0
    with SessionLocal.begin() as s:
        for i in range(0, len(syms), 500):   # SQLite limita las variables por statement
            filas = s.execute(select(UniverseNovedadORM).where(
                UniverseNovedadORM.symbol.in_(syms[i:i + 500]),
                UniverseNovedadORM.estado == "nueva")).scalars().all()
            for f in filas:
                f.estado = "descartada"
                f.updated_at = _ahora()
            n += len(filas)
    return n


def contar_nuevas() -> int:
    init_db()
    with SessionLocal() as s:
        n = s.execute(select(func.count()).select_from(UniverseNovedadORM)
                      .where(UniverseNovedadORM.estado == "nueva")).scalar()
    return int(n or 0)


def simbolos_registrados(s) -> Tuple[Set[str], Set[str]]:
    """(todas, pendientes): lo que ya está en `universe_novedades` y, de eso, lo que
    sigue en `nueva`. `todas` incluye además la ESPECIE (`ticker_pesos` de `byma_catalog`)
    de cada registrada, para que una pata nueva de una especie ya registrada (AEC3C
    después de AEC3D) no sea otra novedad."""
    todas: Set[str] = set()
    pend: Set[str] = set()
    for sym, estado, base in s.execute(
            select(UniverseNovedadORM.symbol, UniverseNovedadORM.estado,
                   BymaCatalogORM.ticker_pesos)
            .outerjoin(BymaCatalogORM, BymaCatalogORM.symbol == UniverseNovedadORM.symbol)
    ).all():
        todas.add(sym)
        if base:
            todas.add(_norm(base))
        if estado == "nueva":
            pend.add(sym)
    return todas, pend


def isins_registrados(s) -> Set[str]:
    """ISINs (según `byma_catalog`) de todo lo que ya está en `universe_novedades`: el
    mismo activo con otro ticker (moneda D/C, ámbito X/Y/Z) no es otra novedad."""
    return {str(isin).upper() for (isin,) in s.execute(
        select(BymaCatalogORM.isin)
        .join(UniverseNovedadORM, UniverseNovedadORM.symbol == BymaCatalogORM.symbol)
        .where(BymaCatalogORM.isin.is_not(None))).all() if isin}


def listar(estado: str = "nueva") -> List[dict]:
    """Novedades en `estado` con la metadata de `byma_catalog` (outer join: una novedad
    sin fila en el universo muestra lo que guardó al detectarse)."""
    init_db()
    with SessionLocal() as s:
        rows = s.execute(
            select(UniverseNovedadORM, BymaCatalogORM)
            .outerjoin(BymaCatalogORM, BymaCatalogORM.symbol == UniverseNovedadORM.symbol)
            .where(UniverseNovedadORM.estado == estado)
            .order_by(UniverseNovedadORM.first_seen.desc(), UniverseNovedadORM.symbol)
        ).all()
    out: List[dict] = []
    for n, u in rows:
        categoria = (u.categoria if u else None) or n.categoria or "Otros"
        out.append({
            "symbol": n.symbol, "first_seen": n.first_seen, "source": n.source,
            "estado": n.estado, "categoria": categoria,
            "panel": u.panel if u else None, "emisor": u.emisor if u else None,
            "denominacion": u.denominacion if u else None, "isin": u.isin if u else None,
            "moneda": u.moneda if u else None, "vencimiento": u.vencimiento if u else None,
            "cargable": categoria not in CATEGORIAS_SIN_HOJA,
        })
    return out


def agrupadas(estado: str = "nueva") -> List[dict]:
    """[{categoria, filas}] ordenado por categoría (estable, para que la pestaña no
    salte de orden entre refrescos)."""
    grupos: Dict[str, List[dict]] = {}
    for f in listar(estado):
        grupos.setdefault(f["categoria"], []).append(f)
    return [{"categoria": c, "filas": fs} for c, fs in sorted(grupos.items())]


def leer_meta(key: str) -> Optional[str]:
    """Valor de una clave propia en `schema_meta` (None si no existe). Mismo mecanismo
    que `catalog_repository._stamp_schema_version` y los scripts de backfill."""
    eng = get_engine()
    if not inspect(eng).has_table("schema_meta"):
        return None
    with eng.begin() as conn:
        row = conn.exec_driver_sql("SELECT value FROM schema_meta WHERE key=?",
                                   (key,)).fetchone()
    return row[0] if row else None


def escribir_meta_en(s, key: str, value) -> None:
    """Upsert de una clave propia en `schema_meta`, dentro de la transacción `s`."""
    s.connection().exec_driver_sql(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
