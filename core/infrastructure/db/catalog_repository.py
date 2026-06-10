"""CatalogRepository: implementación de IInstrumentsRepository sobre SQLite.

Drop-in de `ExcelInstrumentsRepository` (mismas firmas). Lee de SQLite y cachea
en memoria al instanciar. Si la base está vacía, auto-siembra desde el Excel
(reusando el parsing probado del repo Excel) — así el cutover no requiere correr
`ingest_master.py` a mano la primera vez. `reload()` re-siembra desde el Excel,
manteniendo paridad con el flujo actual (la ABM edita el Excel y dispara reload).
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Dict, List, Optional

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
CURRENT_SCHEMA_VERSION = 1


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


def reseed_with_meta(rows) -> int:
    """Wipe + reseed — transaccional, idempotente. Cada fila es UN bono:
    (Instrument, sheet, raw_fields[, secondary_tickers]). Las patas de moneda
    secundarias se guardan en ticker_mep/ticker_ccl (1 fila por bono)."""
    from core.infrastructure.repositories import split_currency_tickers

    init_db()
    rows = [tuple(r) for r in rows]
    with SessionLocal.begin() as s:
        s.execute(delete(CashflowORM))
        s.execute(delete(InstrumentORM))
        for r in rows:
            inst, sheet, raw = r[0], r[1], r[2]
            secondaries = r[3] if len(r) > 3 else []
            _, mep, ccl = split_currency_tickers([inst.ticker, *(secondaries or [])])
            s.add(instrument_to_orm(inst, sheet, raw, ticker_mep=mep, ticker_ccl=ccl))
    return len(rows)


def ingest_from_excel(xlsx_path: str) -> int:
    """Excel → SQLite. Reusa el parsing probado de ExcelInstrumentsRepository,
    preservando sheet + raw_fields para el round-trip del form del ABM."""
    from core.infrastructure.repositories import ExcelInstrumentsRepository
    triples = ExcelInstrumentsRepository(xlsx_path).get_all_with_meta()
    n = reseed_with_meta(triples)
    logger.info("ingest_from_excel: seeded %d instruments into SQLite.", n)
    return n


def _orm_to_domain(orm: InstrumentORM) -> Instrument:
    return Instrument(
        ticker=orm.ticker, short_name=orm.short_name, instrument_type=orm.instrument_type,
        maturity_date=orm.maturity_date, emission_date=orm.emission_date,
        cashflows=[
            Cashflow(date=cf.fecha_pago, amortization=cf.amortizacion, interest=cf.cupon_interes)
            for cf in orm.cashflows
        ],
        cer_base=orm.cer_base, cer_lag=orm.cer_lag, category=orm.category,
        floor_rate_monthly=orm.floor_rate_monthly, spread_rate=orm.spread_rate,
        cer_spread=orm.cer_spread, payment_frequency=orm.payment_frequency,
        day_count=orm.day_count, isin=getattr(orm, "isin", None),
        # ley_aplicable vive en raw_fields (no es columna ORM) — la lee el motor para
        # elegir MEP (ley AR) vs CCL (Extranjera) en la pata pesos de ONs hard-dollar.
        ley_aplicable=(orm.raw_fields or {}).get("ley_aplicable") or None,
    )


class CatalogRepository(IInstrumentsRepository):
    def __init__(self, xlsx_path: Optional[str] = None, auto_seed: bool = True):
        from config.settings import settings
        self._xlsx_path = xlsx_path or str(settings.master_xlsx)
        self._cache: List[Instrument] = []
        self._by_ticker: Dict[str, Instrument] = {}
        self._by_type: Dict[str, List[Instrument]] = {}
        init_db()
        if auto_seed and self._is_empty():
            ingest_from_excel(self._xlsx_path)
        self._load()

    def _is_empty(self) -> bool:
        with SessionLocal() as s:
            return s.execute(select(InstrumentORM.ticker).limit(1)).first() is None

    def _load(self) -> None:
        from core.infrastructure.repositories import expand_currency_legs

        with SessionLocal() as s:
            orms = s.execute(select(InstrumentORM)).scalars().all()
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

    def reload(self, reseed_from_excel: bool = True) -> None:
        if reseed_from_excel:
            ingest_from_excel(self._xlsx_path)
        self._load()

    # IInstrumentsRepository ------------------------------------------------ #
    def get_all_instruments(self) -> List[Instrument]:
        return self._cache

    def get_instruments_by_type(self, instrument_type: str) -> List[Instrument]:
        return list(self._by_type.get(instrument_type, ()))

    def get_instrument_by_ticker(self, ticker: str) -> Optional[Instrument]:
        return self._by_ticker.get(ticker)
