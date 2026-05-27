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
from typing import Dict, List, Optional

from sqlalchemy import delete, inspect, select

from core.domain.interfaces import IInstrumentsRepository
from core.domain.models import Cashflow, Instrument
from core.infrastructure.db.engine import SessionLocal, get_engine
from core.infrastructure.db.models import Base, CashflowORM, InstrumentORM

logger = logging.getLogger(__name__)


def init_db() -> None:
    """Crea las tablas si faltan. Si la tabla `instruments` existe pero le faltan
    columnas nuevas (sheet/raw_fields del ABM), la recrea — la .db es solo un
    cache derivado del Excel, así que dropear y re-sembrar es seguro."""
    eng = get_engine()
    Base.metadata.create_all(eng)
    insp = inspect(eng)
    if insp.has_table("instruments"):
        cols = {c["name"] for c in insp.get_columns("instruments")}
        if not {"sheet", "raw_fields"} <= cols:
            logger.info("catalog schema drift: recreando tablas (faltan sheet/raw_fields).")
            Base.metadata.drop_all(eng)
            Base.metadata.create_all(eng)


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
                      raw_fields: Optional[dict] = None) -> InstrumentORM:
    """Domain Instrument (+ meta del ABM) → InstrumentORM con cashflows materializados."""
    orm = InstrumentORM(
        ticker=inst.ticker, short_name=inst.short_name,
        instrument_type=inst.instrument_type,
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


def reseed(instruments: List[Instrument]) -> int:
    """Wipe + reseed SQLite desde domain Instruments (sin meta del ABM)."""
    return reseed_with_meta([(i, None, None) for i in instruments])


def reseed_with_meta(triples) -> int:
    """Wipe + reseed desde (Instrument, sheet, raw_fields) — transaccional, idempotente."""
    init_db()
    triples = list(triples)
    with SessionLocal.begin() as s:
        s.execute(delete(CashflowORM))
        s.execute(delete(InstrumentORM))
        for inst, sheet, raw in triples:
            s.add(instrument_to_orm(inst, sheet, raw))
    return len(triples)


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
        day_count=orm.day_count,
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
        with SessionLocal() as s:
            orms = s.execute(select(InstrumentORM)).scalars().all()
            insts = [_orm_to_domain(o) for o in orms]
        self._cache = insts
        self._by_ticker = {i.ticker: i for i in insts}
        by_type: Dict[str, List[Instrument]] = {}
        for i in insts:
            by_type.setdefault(i.instrument_type, []).append(i)
        self._by_type = by_type
        logger.info("CatalogRepository loaded %d instruments from SQLite.", len(insts))

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
