"""CatalogRepository: implementación de IInstrumentsRepository sobre SQLite.

Drop-in de `ExcelInstrumentsRepository` (mismas firmas). Lee de SQLite y cachea
en memoria al instanciar. Si la base está vacía, auto-siembra desde el Excel
(reusando el parsing probado del repo Excel) — así el cutover no requiere correr
`ingest_master.py` a mano la primera vez. `reload()` solo refresca el cache en
memoria desde SQLite (NUNCA re-siembra): la ABM escribe SQLite directo y dispara
`reload()` en caliente; el re-seed destructivo vive solo en `ingest_from_excel`.
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import OperationalError

from core.domain.interfaces import IInstrumentsRepository
from core.domain.models import Cashflow, Instrument
from core.infrastructure.db.engine import SessionLocal, get_engine
from core.infrastructure.db.models import Base, CashflowORM, InstrumentORM

logger = logging.getLogger(__name__)

# Versión del schema del catálogo. Subir SOLO cuando se introduce una migración de
# DATOS (no basta agregar columnas — eso lo reconcilia _migrate_table_add_columns
# de forma aditiva en cada arranque). Sirve de punto de control para backups y para
# correr transformaciones de datos exactamente una vez.
#   v2 (2026-09-07): `_migrate_v2_bopreal_huerfanos` — BOPREAL con tipo huérfano
#        retipados desde la ficha BYMA guardada en `raw_fields` (caso BPOA8).
#   v3 (2026-09-07): `_migrate_v3_deshacer_primera_corrida_del_universo` — deshace la
#        primera corrida del job de novedades (contó el esqueleto BYMA con precio 0 y las
#        variantes .SB/X/Y/Z como especies) y lo deja listo para correr con las reglas
#        corregidas.
#   v4 (2026-09-07): `_migrate_v4_quitar_patas_de_ambito` — patas X/Y/Z de renta fija
#        que la segunda corrida insertó como `primary`/`cotiza=1` (B2N6X, BAF7X…) y
#        sus novedades.
CURRENT_SCHEMA_VERSION = 4


def _ensure_schema_meta(eng) -> None:
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR)"
        )


def get_schema_version() -> int:
    """Versión de schema sellada en la DB. 0 si nunca se selló (DB pre-versionado o
    recién creada) — no rompe, permite detectar el caso y migrar."""
    eng = get_engine()
    if not inspect(eng).has_table("schema_meta"):
        return 0
    with eng.begin() as conn:
        row = conn.exec_driver_sql(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _stamp_schema_version(eng, version: int) -> None:
    _ensure_schema_meta(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(version),),
        )


def _migrate_table_add_columns(eng, table) -> None:
    """Reconcilia una tabla existente con su modelo ORM agregando SOLO las columnas
    faltantes (`ALTER TABLE ... ADD COLUMN`). FORWARD-ONLY: nunca dropea ni modifica
    columnas existentes. Es seguro sobre tablas con datos — las altas de la ABM
    (ON/Acciones que viven solo en la DB) sobreviven cualquier drift de schema (C1).

    Las columnas nuevas se agregan nullable; si el ORM define un default escalar y la
    columna es NOT NULL, se agrega con `DEFAULT` (SQLite exige default para NOT NULL
    sobre tablas no vacías). Las PK se saltean (SQLite no permite ALTER ADD de PK, y
    una tabla existente siempre conserva la suya)."""
    insp = inspect(eng)
    if not insp.has_table(table.name):
        return  # create_all ya la habrá creado entera
    existing = {c["name"] for c in insp.get_columns(table.name)}
    for col in table.columns:
        if col.name in existing or col.primary_key:
            continue
        type_sql = col.type.compile(dialect=eng.dialect)
        ddl = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {type_sql}'
        default = getattr(col.default, "arg", None) if col.default is not None else None
        if default is not None and not callable(default):
            if isinstance(default, str):
                # Literal SQL: duplicar comillas simples ('' es el escape de SQLite).
                # Sin esto, un default futuro tipo "won't" rompe el DDL en el boot.
                lit = "'" + default.replace("'", "''") + "'"
            else:
                lit = str(default)
            ddl += f" DEFAULT {lit}"
            if not col.nullable:
                ddl += " NOT NULL"
        try:
            with eng.begin() as conn:
                conn.exec_driver_sql(ddl)
        except OperationalError as e:
            # Carrera benigna entre dos procesos migrando la misma DB (script +
            # server arrancando): el otro ya agregó la columna — objetivo cumplido.
            # Cualquier otro fallo de ALTER sí debe propagar (drift no manejable).
            if "duplicate column" not in str(e).lower():
                raise
            logger.info("catalog: columna %s.%s ya agregada por otro proceso (carrera benigna).",
                        table.name, col.name)
            continue
        logger.info("catalog: ALTER %s ADD COLUMN %s (migración aditiva, sin pérdida).",
                    table.name, col.name)


_MIGRACION_V2_EMISOR = "banco central"


def _fecha_ficha(v):
    from datetime import date as _d
    try:
        return _d.fromisoformat(str(v).strip()[:10]) if v else None
    except ValueError:
        return None


def _migrate_v2_bopreal_huerfanos(eng) -> int:
    """v1 → v2: retipa como BOPREAL las filas de la hoja Soberanos con `instrument_type`
    HUÉRFANO cuya ficha BYMA guardada en `raw_fields["byma"]` dice emisor BCRA, y les
    repone vencimiento y emisión desde esa misma ficha (sólo lo que está vacío).

    Origen: BPOA8 (BOPREAL Serie 4A) quedó `SOBERANOS` y sin `maturity_date` tras un
    round-trip del ABM (`scripts/migrate_orphan_types.py` cuenta la historia); un tipo
    huérfano deja el bono INVISIBLE en todos los paneles y es el «1 orphan» de
    `/api/health`. El script lo arregla a mano desde la semilla IAMC, pero prod se toca
    sólo por `deploy.sh`: esta es la vía versionada de CLAUDE.md, corre sola al arrancar
    y exactamente una vez por DB. NO adivina: otro emisor, otra hoja o una fila sin
    ficha quedan como están (las resuelve el operador por ABM) y un tipo ya válido no se
    toca. FORWARD-ONLY: sólo UPDATE de columnas existentes. Devuelve cuántas filas tocó."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from sqlalchemy.orm.attributes import flag_modified

    from core.domain.instrument_groups import is_known_type

    n = 0
    with Session(eng) as s, s.begin():
        for o in s.execute(select(InstrumentORM)).scalars():
            if is_known_type(o.instrument_type or ""):
                continue
            if (o.sheet or "").strip().lower() != "soberanos":
                continue
            raw = dict(o.raw_fields or {})
            byma = raw.get("byma") or {}
            if _MIGRACION_V2_EMISOR not in str(byma.get("emisor") or "").lower():
                continue
            ficha = byma.get("ficha") or {}
            vto = _fecha_ficha(ficha.get("fecha_vencimiento"))
            emi = _fecha_ficha(ficha.get("fecha_emision"))
            viejo = o.instrument_type
            o.instrument_type = "BOPREAL"
            o.day_count = "30/360"                 # convención BOPREAL (prospecto BCRA)
            if o.maturity_date is None and vto:
                o.maturity_date = vto
            if o.emission_date is None and emi:
                o.emission_date = emi
            # También al blob del form (MERGE, nunca reemplazo): sin `tipo` el próximo
            # round-trip del ABM volvería a perderlo.
            raw["tipo"] = "BOPREAL"
            if vto and not raw.get("fecha_vencimiento"):
                raw["fecha_vencimiento"] = vto.isoformat()
            o.raw_fields = raw
            flag_modified(o, "raw_fields")
            logger.warning("catalog v2: %s retipado %s -> BOPREAL desde la ficha BYMA "
                           "(vto %s, emisión %s).", o.ticker, viejo, vto, emi)
            n += 1
    return n


_META_UNIVERSE_KEYS = ("universe_ultima_corrida", "universe_ultimos_vistos")


def _simbolos_del_seed() -> set:
    """Símbolos del CSV semilla de `byma_catalog` (vacío si el CSV no está a mano)."""
    import csv
    from pathlib import Path

    from config.settings import settings

    path = Path(settings.byma_catalog_csv) if getattr(settings, "byma_catalog_csv", None) else None
    if not path or not path.is_file():
        return set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {(r.get("symbol") or "").upper().strip()
                for r in csv.DictReader(f, delimiter=";") if (r.get("symbol") or "").strip()}


def _migrate_v3_deshacer_primera_corrida_del_universo(eng) -> dict:
    """v2 → v3: deshace la primera corrida del job de novedades del universo (prod,
    2026-09-07 22:15), que tomó como «vistos» los símbolos con precio 0 del maestro de
    BYMA y como novedades las variantes (.SB, X/Y/Z, patas D/C): +4410 filas en
    `byma_catalog` —17 con el ISIN de un bono cargado y `cotiza=1`, que
    `backfill_legs_from_universe` habría escrito en `instruments` en el próximo
    arranque— y 4155 «novedades».

    Deja la tabla como la sembró el CSV y al job listo para volver a correr con las reglas
    corregidas (`novedades` regla 4, `universe_service._cotizo`): borra de `byma_catalog`
    las filas con `last_seen` que NO están en el seed (sólo el job las pudo agregar), pone
    `last_seen` en NULL, borra las novedades `nueva` (sin decisión del operador;
    `cargada`/`descartada` se conservan) y las claves `universe_*` de `schema_meta` (el
    guard relativo había quedado sellado en 8747 vistos: con ~1500 reales rechazaría
    todas las corridas). Sin el CSV a mano no se distingue seed de job: se borra sólo el
    subconjunto dañino (`.SB` y X/Y/Z con `last_seen`) y se avisa. FORWARD-ONLY en el
    catálogo de pricing: no toca `instruments` ni `cashflows`."""
    out = {"catalogo": 0, "novedades": 0, "sin_seed": False}
    with eng.begin() as conn:
        vistos = [r[0] for r in conn.exec_driver_sql(
            "SELECT symbol FROM byma_catalog WHERE last_seen IS NOT NULL").fetchall()]
        pendientes = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM universe_novedades WHERE estado='nueva'").scalar() or 0
        metas = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM schema_meta WHERE key IN (?, ?)", _META_UNIVERSE_KEYS
        ).scalar() or 0
        if not vistos and not pendientes and not metas:
            return out                       # el job nunca corrió acá: nada que deshacer
        seed = _simbolos_del_seed()
        if seed:
            borrar = [s for s in vistos if (s or "").upper() not in seed]
        else:
            out["sin_seed"] = True
            borrar = [s for s in vistos
                      if (s or "").upper().endswith(".SB")
                      or (len(s or "") == 5 and (s or "")[-1].upper() in "XYZ")]
        for i in range(0, len(borrar), 500):     # SQLite limita las variables por statement
            chunk = borrar[i:i + 500]
            conn.exec_driver_sql(
                "DELETE FROM byma_catalog WHERE symbol IN (%s)" % ",".join("?" * len(chunk)),
                tuple(chunk))
        conn.exec_driver_sql("UPDATE byma_catalog SET last_seen=NULL WHERE last_seen IS NOT NULL")
        res = conn.exec_driver_sql("DELETE FROM universe_novedades WHERE estado='nueva'")
        conn.exec_driver_sql("DELETE FROM schema_meta WHERE key IN (?, ?)", _META_UNIVERSE_KEYS)
        out["catalogo"] = len(borrar)
        out["novedades"] = int(res.rowcount or 0)
    return out


def _migrate_v4_quitar_patas_de_ambito(eng) -> dict:
    """v3 → v4: quita de `byma_catalog` las patas de otro ámbito (sufijo X/Y/Z) de renta
    fija (`security_type` GO/CORP) que el job insertó como `primary`/`cotiza=1` —la regla
    «raíz compartida» fallaba el día en que sólo cotizaba la pata X (B2N6X, BAF7X, SE7X…,
    varias con el ISIN de un bono cargado: `backfill_legs_from_universe` las habría tomado
    como pata pesos)— y sus novedades `nueva`. Sólo filas `primary` con `last_seen`: las
    del seed CSV vienen como `especial`/`cotiza=0` y no se tocan. Equities no entran:
    NFLX/SPCX/SKHY son tickers reales.

    En prod corrió (2026-09-08 07:29 AR) una versión SIN el filtro `primary` y se llevó
    además 522 patas `especial` del seed que el job había marcado como vistas: filas
    inertes (`cotiza=0`: fuera de `_universe_groups` y de la búsqueda del Universo), que
    el CSV conserva. Se deja constancia; no se restauran."""
    out = {"catalogo": 0, "novedades": 0}
    with eng.begin() as conn:
        syms = [r[0] for r in conn.exec_driver_sql(
            "SELECT symbol FROM byma_catalog WHERE last_seen IS NOT NULL "
            "AND security_type IN ('GO', 'CORP') AND clase_liquidacion = 'primary'").fetchall()]
        borrar = [s for s in syms if len(s or "") >= 4 and (s or "")[-1].upper() in "XYZ"]
        for i in range(0, len(borrar), 500):
            chunk = borrar[i:i + 500]
            marcas = ",".join("?" * len(chunk))
            conn.exec_driver_sql("DELETE FROM byma_catalog WHERE symbol IN (%s)" % marcas,
                                 tuple(chunk))
            res = conn.exec_driver_sql(
                "DELETE FROM universe_novedades WHERE estado='nueva' AND symbol IN (%s)" % marcas,
                tuple(chunk))
            out["novedades"] += int(res.rowcount or 0)
        out["catalogo"] = len(borrar)
    return out


# Engine ya inicializado/migrado en este proceso. init_db() se llama ~39 veces
# (cada operación ABM lo invoca defensivamente); tras la primera corrida exitosa
# sobre un engine dado, el resto son no-op — sin re-inspección de schema ni el
# write txn del stamp por click. `configure()` crea un engine NUEVO (los tests
# redirigen la DB así), lo que invalida el flag por identidad. El lock cierra la
# carrera teórica de dos threads en la PRIMERA llamada (rutas ABM en el threadpool
# de Starlette) — sin él ambos podrían migrar a la vez.
_INITIALIZED_ENGINE = None
_INIT_LOCK = threading.Lock()


def init_db() -> None:
    """Crea las tablas que falten y reconcilia las existentes con el modelo ORM de
    forma **forward-only**: agrega columnas nuevas con ALTER, NUNCA dropea.

    Invariante C1: `catalog.db` es la fuente de verdad viva (las altas ABM de
    ON/Acciones viven solo acá, no en el Excel semilla). Un `drop_all` ante drift de
    schema las borraría irreversiblemente, así que está prohibido — toda evolución de
    schema es aditiva. Para transformaciones de datos (no solo columnas nuevas), usar
    una migración explícita versionada, jamás recrear.

    Idempotente y barata: corre una vez por engine (ver _INITIALIZED_ENGINE)."""
    global _INITIALIZED_ENGINE
    eng = get_engine()
    if eng is _INITIALIZED_ENGINE:   # fast-path sin lock (lectura atómica)
        return
    with _INIT_LOCK:
        if eng is _INITIALIZED_ENGINE:   # double-check bajo lock
            return
        Base.metadata.create_all(eng)
        for table in Base.metadata.sorted_tables:
            _migrate_table_add_columns(eng, table)
        # Crear índices aditivos en DBs existentes (create_all los omite si la tabla
        # ya existe). Idempotente via IF NOT EXISTS — orden de columnas no importa.
        with eng.begin() as conn:
            for ddl in (
                "CREATE INDEX IF NOT EXISTS ix_instr_mep ON instruments (ticker_mep)",
                "CREATE INDEX IF NOT EXISTS ix_instr_ccl ON instruments (ticker_ccl)",
                "CREATE INDEX IF NOT EXISTS ix_instr_isin ON instruments (isin)",
                # Email único SOLO cuando hay email: los usuarios sin email (los de
                # antes del Manager v2, o los que se cargan a mano) conviven.
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email ON users (email) "
                "WHERE email IS NOT NULL",
            ):
                conn.exec_driver_sql(ddl)
        # Migraciones de DATOS versionadas: exactamente una vez por DB, en orden, ANTES
        # de sellar la versión vigente (si una falla, la DB queda en la versión anterior
        # y vuelve a intentarse en el próximo arranque).
        _ensure_schema_meta(eng)          # las migraciones la consultan antes del sello
        version = get_schema_version()
        if version < 2:
            n = _migrate_v2_bopreal_huerfanos(eng)
            if n:
                logger.warning("catalog: migración v2 — %d BOPREAL huérfano(s) retipados "
                               "desde la ficha BYMA.", n)
        if version < 3:
            r = _migrate_v3_deshacer_primera_corrida_del_universo(eng)
            if r["catalogo"] or r["novedades"]:
                logger.warning("catalog: migración v3 — deshecha la primera corrida del "
                               "universo: -%d filas de byma_catalog, -%d novedades%s.",
                               r["catalogo"], r["novedades"],
                               " (sin CSV seed: sólo .SB y X/Y/Z)" if r["sin_seed"] else "")
        if version < 4:
            r = _migrate_v4_quitar_patas_de_ambito(eng)
            if r["catalogo"] or r["novedades"]:
                logger.warning("catalog: migración v4 — quitadas %d pata(s) de ámbito X/Y/Z de "
                               "byma_catalog y %d novedad(es).", r["catalogo"], r["novedades"])
        _stamp_schema_version(eng, CURRENT_SCHEMA_VERSION)
        _INITIALIZED_ENGINE = eng


def _num(x: Optional[float]) -> float:
    """Monto saneado: nan/inf/None → 0.0. Algunos cashflows synth de bonos TAMAR
    traen cupon_interes = nan (artefacto del Excel); SQLite no acepta nan y esos
    bonos no se pricean desde cashflows, así que 0.0 es seguro."""
    if x is None:
        return 0.0
    try:
        if math.isnan(x) or math.isinf(x):
            return 0.0
    except TypeError:
        return 0.0
    return float(x)


def instrument_to_orm(inst: Instrument, sheet: Optional[str] = None,
                      raw_fields: Optional[dict] = None,
                      ticker_mep: Optional[str] = None,
                      ticker_ccl: Optional[str] = None) -> InstrumentORM:
    """Domain Instrument (+ meta del ABM + patas de moneda) → InstrumentORM (1 fila
    por bono) con cashflows materializados (bajo el ticker primario)."""
    orm = InstrumentORM(
        ticker=inst.ticker, ticker_mep=ticker_mep, ticker_ccl=ticker_ccl,
        short_name=inst.short_name,
        instrument_type=inst.instrument_type,
        isin=getattr(inst, "isin", None),
        maturity_date=inst.maturity_date, emission_date=inst.emission_date,
        cer_base=inst.cer_base, cer_lag=inst.cer_lag, category=inst.category,
        floor_rate_monthly=inst.floor_rate_monthly, spread_rate=inst.spread_rate,
        cer_spread=inst.cer_spread, payment_frequency=inst.payment_frequency,
        day_count=inst.day_count, sheet=sheet, raw_fields=raw_fields,
    )
    orm.cashflows = [
        CashflowORM(ticker=inst.ticker, fecha_pago=cf.date,
                    amortizacion=_num(cf.amortization), cupon_interes=_num(cf.interest))
        for cf in inst.cashflows
    ]
    return orm


def reseed_with_meta(rows, allow_drop: bool = False) -> int:
    """Wipe + reseed — transaccional, idempotente. Cada fila es UN bono:
    (Instrument, sheet, raw_fields[, secondary_tickers]). Las patas de moneda
    secundarias se guardan en ticker_mep/ticker_ccl (1 fila por bono).

    Guard anti-pérdida: si el re-seed dejaría afuera bonos que HOY están en la
    DB (altas ABM que viven solo en SQLite — el Excel es semilla, no espejo),
    aborta con la lista. `allow_drop=True` = override consciente."""
    from core.infrastructure.repositories import split_currency_tickers

    init_db()
    rows = [tuple(r) for r in rows]
    if not allow_drop:
        incoming = {r[0].ticker for r in rows}
        with SessionLocal() as s:
            existing = {t for (t,) in s.execute(select(InstrumentORM.ticker)).all()}
        lost = sorted(existing - incoming)
        if lost:
            preview = ", ".join(lost[:10]) + ("…" if len(lost) > 10 else "")
            raise ValueError(
                f"re-seed abortado: borraría {len(lost)} bono(s) que viven solo en la "
                f"DB (altas ABM): {preview}. Si es intencional, usar allow_drop=True.")
    with SessionLocal.begin() as s:
        s.execute(delete(CashflowORM))
        s.execute(delete(InstrumentORM))
        for r in rows:
            inst, sheet, raw = r[0], r[1], r[2]
            secondaries = r[3] if len(r) > 3 else []
            _, mep, ccl = split_currency_tickers([inst.ticker, *(secondaries or [])])
            s.add(instrument_to_orm(inst, sheet, raw, ticker_mep=mep, ticker_ccl=ccl))
    return len(rows)


def ingest_from_excel(xlsx_path: str, allow_drop: bool = False) -> int:
    """Excel → SQLite. Reusa el parsing probado de ExcelInstrumentsRepository,
    preservando sheet + raw_fields para el round-trip del form del ABM.
    Hereda el guard anti-pérdida de `reseed_with_meta` (el Excel es semilla:
    NO conoce las altas ABM — re-sembrar sin chequear las borraría)."""
    from core.infrastructure.repositories import ExcelInstrumentsRepository
    triples = ExcelInstrumentsRepository(xlsx_path).get_all_with_meta()
    n = reseed_with_meta(triples, allow_drop=allow_drop)
    logger.info("ingest_from_excel: seeded %d instruments into SQLite.", n)
    return n


def _coupon_pct(raw_fields) -> Optional[float]:
    """Cupón anual nominal % desde raw_fields['cupon anual %'] (string o número); None si vacío."""
    v = (raw_fields or {}).get("cupon anual %")
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return None


_PRECIO_DE = "precio_de:"


def _price_alias(raw) -> Optional[str]:
    """`raw_fields["precio_fallback"] = "precio_de:TY30P"` → `"TY30P"`; cualquier otra
    forma (o la clave ausente) → None. Es la única forma soportada del alias de precio
    (`Instrument.price_alias`): el instrumento cotiza con la cotización de OTRO símbolo.
    La clave la llevaba TY30PUT desde su alta y ningún código la leía (cero precios)."""
    v = str((raw or {}).get("precio_fallback") or "").strip()
    if not v.lower().startswith(_PRECIO_DE):
        return None
    return v[len(_PRECIO_DE):].strip().upper() or None


def _orm_to_domain(orm: InstrumentORM) -> Instrument:
    return Instrument(
        ticker=orm.ticker, short_name=orm.short_name, instrument_type=orm.instrument_type,
        maturity_date=orm.maturity_date, emission_date=orm.emission_date,
        # FILTRO DEL ANCLA — único punto por el que los CashflowORM entran al dominio.
        # Una fila `es_ancla` es el vencimiento declarado de un instrumento de payoff
        # ANALÍTICO (TAMAR PURO/DUAL/DUAL_CER_TAMAR): existe en la DB para que el bono
        # sea auditable y visible en /cashflows, pero NO es un pago. Dejándola afuera
        # acá, esos bonos siguen llegando al motor con `cashflows=()` igual que hoy →
        # el pricing es bit-idéntico POR CONSTRUCCIÓN (no por coincidencia numérica).
        cashflows=[
            Cashflow(date=cf.fecha_pago, amortization=cf.amortizacion, interest=cf.cupon_interes)
            for cf in orm.cashflows if not cf.es_ancla
        ],
        cer_base=orm.cer_base, cer_lag=orm.cer_lag, category=orm.category,
        floor_rate_monthly=orm.floor_rate_monthly, spread_rate=orm.spread_rate,
        cer_spread=orm.cer_spread, payment_frequency=orm.payment_frequency,
        day_count=orm.day_count, isin=getattr(orm, "isin", None),
        # ley_aplicable vive en raw_fields (no es columna ORM) — la lee el motor para
        # elegir MEP (ley AR) vs CCL (Extranjera) en la pata pesos de ONs hard-dollar.
        ley_aplicable=(orm.raw_fields or {}).get("ley_aplicable") or None,
        # serie_clase también vive en raw_fields (display-only): la CLASE de la ON.
        serie_clase=(orm.raw_fields or {}).get("serie_clase") or None,
        coupon_rate=_coupon_pct(orm.raw_fields),   # cupón anual % (display-only)
        sector_override=(orm.raw_fields or {}).get("sector_override") or None,  # categoría manual ABM
        price_alias=_price_alias(orm.raw_fields),   # "precio_de:X" → cotiza con X
    )


class CatalogRepository(IInstrumentsRepository):
    def __init__(self, xlsx_path: Optional[str] = None, auto_seed: bool = True):
        from config.settings import settings
        self._xlsx_path = xlsx_path or str(settings.master_xlsx)
        self._cache: List[Instrument] = []
        self._by_ticker: Dict[str, Instrument] = {}
        self._by_type: Dict[str, List[Instrument]] = {}
        # {"orphans": [...], "defaulted": [...]} del último `_load` (ver `type_health`).
        self._type_health: Dict[str, List[Dict[str, Any]]] = {"orphans": [], "defaulted": []}
        # Motivo del fallo de la SIEMBRA, si la hubo (ver `_seed` / `seed_error`).
        self._seed_error: Optional[str] = None
        init_db()
        if auto_seed and self._is_empty():
            self._seed()
        self._load()

    def _seed(self) -> None:
        """Bootstrap con la DB VACÍA: sembrar el catálogo desde el Excel semilla.

        CONTRATO (explícito, porque cambió y el cambio no estaba flageado):

        · `ingest_from_excel` **LANZA** si el Excel no aportó ni un instrumento
          (semilla ausente/ilegible, 0 filas parseables). Eso está BIEN y se
          mantiene: antes devolvía `[]` y se sembraba un catálogo vacío en silencio
          —el guard anti-pérdida de `reseed_with_meta` no puede disparar con la DB
          vacía, que es justo el bootstrap de un droplet nuevo— y la app arrancaba
          con 0 bonos sin que nada lo dijera.

        · Ese `raise` NO puede propagar desde el constructor. Al hacerlo convertía un
          problema de DATOS en un fallo de ARRANQUE del proceso entero: `get_repo()`
          reventaba, el lifespan moría y no quedaba en pie ni `/login` ni
          `/api/health` — o sea, la app perdía justo la superficie donde el operador
          leería el motivo. Además es un cambio de comportamiento respecto del
          arranque histórico (que levantaba degradado) que nadie declaró.

        Resolución: el fallo se ATRAPA acá, se grita a ERROR, queda en `seed_error` y
        el arranque lo publica en `AppState` → `/api/health` (`catalog.seed_failed`) +
        badge del header. La app levanta con el catálogo vacío, ruidosa y diagnosticable,
        y la DB no queda contaminada con una siembra a medias (la transacción de
        `reseed_with_meta` ni siquiera llegó a abrirse)."""
        try:
            ingest_from_excel(self._xlsx_path)
        except Exception as e:  # noqa: BLE001 — el motivo se publica, no se traga
            self._seed_error = f"{type(e).__name__}: {e}"
            logger.error(
                "catálogo VACÍO: la siembra desde %s falló (%s). La app arranca sin "
                "instrumentos — se publica en /api/health (catalog.seed_failed) y en el "
                "badge del header. Arreglá la semilla o restaurá un backup y reiniciá.",
                self._xlsx_path, self._seed_error)

    def _is_empty(self) -> bool:
        with SessionLocal() as s:
            return s.execute(select(InstrumentORM.ticker).limit(1)).first() is None

    def _load(self) -> None:
        from core.infrastructure.repositories import audit_catalog_types, expand_currency_legs

        with SessionLocal() as s:
            orms = s.execute(select(InstrumentORM)).scalars().all()
            # Señal de salud del catálogo, sobre las filas que ya tenemos en la mano
            # (sin una segunda query): tipos huérfanos (bono cargado pero invisible en
            # todos los paneles) + tipos ASUMIDOS por default de hoja ambiguo (bono
            # visible pero con la strategy de pricing posiblemente equivocada). Deja
            # WARNING en el log; el reporte queda expuesto en `type_health` para que
            # el arranque/health lo publique.
            self._type_health = audit_catalog_types(orms)
            # 1 fila por bono → expandir a una especie por ticker (primario + mep/ccl).
            insts: List[Instrument] = []
            for o in orms:
                primary = _orm_to_domain(o)
                secondaries = [t for t in (o.ticker_mep, o.ticker_ccl) if t]
                insts.extend(expand_currency_legs(primary, secondaries))
        n_bonos = len(orms)
        self._cache = insts
        self._by_ticker = {i.ticker: i for i in insts}
        by_type: Dict[str, List[Instrument]] = {}
        for i in insts:
            by_type.setdefault(i.instrument_type, []).append(i)
        self._by_type = by_type
        logger.info("CatalogRepository loaded %d instruments (%d bonos) from SQLite.",
                    len(insts), n_bonos)

    def reload(self) -> None:
        """Refresca el cache en memoria desde SQLite. NUNCA re-siembra: el camino
        destructivo (wipe + seed desde el Excel) vive solo en `ingest_from_excel` /
        `ingest_master.py`, con sus guards anti-pérdida de altas ABM — un flag de
        re-seed acá era el footgun exacto que esos guards tapan."""
        self._load()

    @property
    def type_health(self) -> Dict[str, List[Dict[str, Any]]]:
        """Reporte de tipos del último `_load`: {"orphans", "defaulted"}.

        `orphans` = bonos que NINGÚN panel muestra (tipo fuera de
        `instrument_groups`); `defaulted` = bonos cuyo tipo se ASUMIÓ del default de
        una hoja ambigua (ON sin `tipo` → hard-dollar). Ambos ya se loguearon como
        WARNING al cargar.

        CABLEADO (ya no es un reporte que sólo leen los tests): el lifespan de
        `apps/web/app.py` lo publica en `AppState.set_catalog_health` apenas warmea el
        repo → sale en `/api/health` (bloque `catalog`). Sin ese consumidor, las filas
        huérfanas volvían a ser invisibles EN SILENCIO, que es exactamente el patrón
        que dejó vivo el bug original."""
        return self._type_health

    @property
    def seed_error(self) -> Optional[str]:
        """Motivo del fallo de la siembra de bootstrap, o None si no hubo que sembrar
        (DB con datos) o la siembra salió bien. Ver `_seed` para el contrato."""
        return self._seed_error

    # IInstrumentsRepository ------------------------------------------------ #
    def get_all_instruments(self) -> List[Instrument]:
        return self._cache

    def get_instruments_by_type(self, instrument_type: str) -> List[Instrument]:
        return list(self._by_type.get(instrument_type, ()))

    def get_instrument_by_ticker(self, ticker: str) -> Optional[Instrument]:
        return self._by_ticker.get(ticker)
