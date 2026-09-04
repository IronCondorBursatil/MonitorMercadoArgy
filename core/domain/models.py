"""Modelos de dominio (Pydantic v2).

`Cashflow` e `Instrument` son **frozen**: se construyen 1× al cargar el catálogo
y el hot-path de pricing sólo lee. `MarketSnapshot` es **mutable** a propósito:
el provider lo crea sin `instrument` y el use-case se lo asigna post-fetch
(`generate_report._execute`: `snapshot.instrument = inst`).

`InstrumentMetrics` sigue siendo `dataclass`: es un contenedor de salida que el
server muta vía `dataclasses.replace`; convertirlo no aporta y rompería esos sitios.

**Clasificación**: las properties (`is_cer`, `is_dolar_linked`, …) replican
EXACTAMENTE las funciones `_is_*_type` que vivían en `services.py`. Comparan sobre
`norm_type` (upper+strip) sin mutar el campo almacenado — el repositorio ya
normaliza `instrument_type` a mayúsculas, así que el resultado es idéntico.

Construcción posicional: los modelos Pydantic no aceptan args posicionales por
defecto, pero código legacy y tests hacen `Cashflow(fecha, amortization=…)` →
se preserva con un `__init__` compatible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, PrivateAttr, field_validator

_UNSET = object()

# `core.domain.daycount` no se puede importar a nivel de módulo (ciclo
# models→daycount→cashflow_synth→models), pero pagar el `from ... import` DENTRO de
# `year_fraction_to` cuesta ~200 ns × ~13.300 llamadas por ciclo de pricing. Se cablea
# UNA vez, perezosamente, en esta celda module-level.
_YEAR_FRACTION = [None]


class Cashflow(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: _date
    amortization: float = 0.0
    interest: float = 0.0

    def __init__(self, date=_UNSET, amortization=_UNSET, interest=_UNSET, **data):
        # Compat posicional: Cashflow(fecha, amortization=…, interest=…).
        if date is not _UNSET:
            data["date"] = date
        if amortization is not _UNSET:
            data["amortization"] = amortization
        if interest is not _UNSET:
            data["interest"] = interest
        super().__init__(**data)

    @property
    def total(self) -> float:
        return self.amortization + self.interest


# Grafías cortas de "ley argentina" que aparecen en el campo `ley_aplicable`
# (cargas por script). Se comparan por IGUALDAD, no por substring: "AR" como
# substring matchearía "ARGELIA"/"HARVARD" y "EXTRANJERA" contiene "AR".
_LEY_ARGENTINA_ALIAS = frozenset({"AR", "ARG", "ARGY", "LOC"})


class Instrument(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    short_name: str
    instrument_type: str  # CER, LECAP, BONAR, etc.
    maturity_date: Optional[_date] = None
    emission_date: Optional[_date] = None  # Needed by TAMAR engine for accrual factor
    cashflows: tuple[Cashflow, ...] = ()
    cer_base: Optional[float] = None
    cer_lag: int = 10  # Default 10 business days for AR CER bonds
    category: Optional[str] = None  # Market-facing label (e.g. "BONCERES CERO CUPON")
    floor_rate_monthly: Optional[float] = None  # DUAL TAMAR: monthly floor rate (decimal)
    spread_rate: Optional[float] = None  # TAMAR PURO/DUAL: annual spread over TAMAR
    cer_spread: Optional[float] = None  # DUAL CER/TAMAR: annual spread over CER (TXMJ* series)
    payment_frequency: int = 2  # Annual coupons (2 = semestral, AR market default)
    day_count: str = "ACT/365.25"  # "30/360", "ACT/365", "ACT/365.25", "ACT/ACT"
    ley_aplicable: Optional[str] = None  # ON: "Argentina" / "Extranjera" — elige MEP vs CCL p/ la pata pesos
    isin: Optional[str] = None  # clave del activo (BYMA); display-only, el motor lo ignora
    serie_clase: Optional[str] = None  # ON: "Clase XXXI" / "Serie 13 Clase A"; display-only (vive en raw_fields)
    coupon_rate: Optional[float] = None  # cupón anual nominal % (raw_fields "cupon anual %"); display-only
    sector_override: Optional[str] = None  # ON: sector elegido a mano en ABM (raw_fields["sector_override"])

    # ------------------------------------------------------------------ #
    # Memos de pricing. Viven en `__pydantic_private__` (mutable aunque el modelo
    # sea frozen) y mueren con el objeto → `CatalogRepository.reload()` los tira
    # solo, sin ciclo de vida que gestionar.
    #   `_pricing_memo`: resultados de las funciones puras de pricing/metrics.py
    #                    keyed por (fn, ref_date, campos de los que dependen).
    #   `_dc_memo`      : (instrument_type, day_count, DayCount) de `day_count_enum`.
    # ------------------------------------------------------------------ #
    _pricing_memo: dict = PrivateAttr(default_factory=dict)
    _dc_memo: tuple = PrivateAttr(default=())

    def model_copy(self, *, update=None, deep=False):
        """`model_copy` COMPARTE los VALORES de `__pydantic_private__` con el original
        (el dict externo se copia, los objetos de adentro no — verificado en
        pydantic 2.13). Los clones de `recompute_as_tamar_puro` /
        `bond_detail._apply_leg` cambian `instrument_type`, que SÍ mueve las métricas
        (BOPREAL descuenta 30/360, su clon "PURO" no) → sin este reset el clon y el
        original se envenenarían mutuamente por el memo compartido.

        Las claves del memo incluyen igual `instrument_type`/`day_count` (defensa en
        profundidad para clones hechos por otro camino, ej. `copy.copy`)."""
        clone = super().model_copy(update=update, deep=deep)
        priv = clone.__pydantic_private__
        if priv is not None:
            priv["_pricing_memo"] = {}
            priv["_dc_memo"] = ()
        return clone

    @field_validator("cashflows", mode="after")
    @classmethod
    def _sort_cashflows(cls, cfs: tuple) -> tuple:
        """INVARIANTE: el schedule de cashflows es cronológico. Un bono paga sus
        flujos en orden de fecha por definición — codificarlo acá (1 sort al
        construir, sort estable) elimina los `sorted()` defensivos dispersos en el
        hot-path de pricing. `model_construct` no se usa en el repo → sin bypass."""
        return tuple(sorted(cfs, key=lambda cf: cf.date))

    # ------------------------------------------------------------------ #
    # Clasificación (espejo 1:1 de services._is_*_type). No muta el campo;
    # norm_type aplica upper+strip igual que las funciones originales.
    # ------------------------------------------------------------------ #
    @property
    def norm_type(self) -> str:
        return (self.instrument_type or "").upper().strip()

    @property
    def is_bopreal(self) -> bool:
        return "BOPREAL" in self.norm_type

    @property
    def is_cer(self) -> bool:
        # Excluye variantes TAMAR (DUAL_CER_TAMAR contiene "CER" pero no es CER).
        t = self.norm_type
        if "TAMAR" in t:
            return False
        return any(token in t for token in ("CER", "CON CUPON", "STEP-UP"))

    @property
    def is_dolar_linked(self) -> bool:
        # Acepta ambas grafías: "DOLAR LINKED" (master, ES) y "DOLLAR LINKED" (ABM ON, EN).
        t = self.norm_type.replace("DOLLAR", "DOLAR")
        return "DOLAR_LINKED" in t or "DOLAR LINKED" in t

    @property
    def is_hard_dollar(self) -> bool:
        """ON hard-dollar (paga USD). La pata pesos (…O) se pasa a USD por MEP/CCL
        según ley (≠ DOLAR_LINKED, que usa el oficial). Disjunto de is_dolar_linked."""
        return "HARD DOLLAR" in self.norm_type

    @property
    def is_ley_argentina(self) -> bool:
        """Ley local explícita → la pata pesos se valúa contra MEP (dólar bolsa).
        Extranjera o SIN dato → CCL (cable): el universo ON es mayormente ley NY.

        Normalización DEFENSIVA de las grafías que existen de verdad en el campo:
        el form ABM y `on_catalog` escriben "Argentina"/"Extranjera" y
        `byma/universe` mapea "Ley Local" → "Argentina", pero hay scripts de carga
        que escribieron abreviaturas — en la catalog.db viva hay una fila con
        `'ARG'` (y `scratch/gen_prov_cba_caba.py` sigue emitiendo esa grafía).
        Con el chequeo viejo (`"ARGENTIN" in ...`) `'ARG'`/`'AR'`/`'Local'` caían
        del lado EXTRANJERO y la pata pesos se dolarizaba al CCL en vez del MEP —
        un error silencioso del tamaño de la brecha MEP/CCL en precio/TIR/paridad.
        """
        v = (self.ley_aplicable or "").upper().strip().rstrip(".")
        if not v:
            return False
        return ("ARGENTIN" in v or "LOCAL" in v
                or v in _LEY_ARGENTINA_ALIAS
                or v.removeprefix("LEY ").strip() in _LEY_ARGENTINA_ALIAS)

    @property
    def is_tamar_puro(self) -> bool:
        return self.norm_type == "PURO"

    @property
    def is_dual_tamar(self) -> bool:
        return self.norm_type == "DUAL"

    @property
    def is_dual_cer_tamar(self) -> bool:
        """TXMJ* series: bullet bond paying max(CER+spread, TAMAR+spread) at maturity."""
        return self.norm_type == "DUAL_CER_TAMAR"

    @property
    def is_30_360(self) -> bool:
        return "30/360" in (self.day_count or "") or self.is_bopreal

    @property
    def day_count_enum(self) -> "DayCount":  # noqa: F821 — forward-ref (import function-local rompe ciclo)
        """Convención de día-count para DESCONTAR (TIR/duration/PV). BOPREAL fuerza
        30/360 (espeja el fallback de `is_30_360`). Import function-local: rompe el
        ciclo models→daycount→cashflow_synth→models.

        MEMOIZADA por instrumento: `year_fraction_to` la consulta POR CASHFLOW
        (~13.300 veces por ciclo de pricing) y cada consulta pagaba el import, la
        property `is_bopreal` (upper+strip del tipo) y el parseo del string. La clave
        es `(instrument_type, day_count)` — los dos únicos campos de los que depende —
        así un clon con otro tipo NUNCA lee la entrada del original."""
        priv = self.__pydantic_private__
        if priv is not None:
            memo = priv.get("_dc_memo")
            if memo and memo[0] == self.instrument_type and memo[1] == self.day_count:
                return memo[2]
        from core.domain.daycount import DayCount, parse_day_count
        dc = DayCount.THIRTY_360 if self.is_bopreal else parse_day_count(self.day_count)
        if priv is not None:
            priv["_dc_memo"] = (self.instrument_type, self.day_count, dc)
        return dc

    def year_fraction_to(self, target: _date, ref: _date) -> float:
        """Fracción de año ref→target bajo la convención del instrumento. Es el
        único punto por el que las strategies/metrics descuentan."""
        yf = _YEAR_FRACTION[0]
        if yf is None:                       # 1× por proceso (ver _YEAR_FRACTION)
            from core.domain.daycount import year_fraction as yf
            _YEAR_FRACTION[0] = yf
        return yf(ref, target, self.day_count_enum)

    def get_future_cashflows(self, reference_date: _date) -> List[Cashflow]:
        # Ex-cupón: un flujo que paga EXACTAMENTE en la fecha de referencia
        # (liquidación) lo cobra el VENDEDOR — el comprador liquida ese día y no es
        # tenedor de registro. Por eso el corte es estricto (> ref). Ver
        # core.domain.pricing.metrics.period_bounds.
        return [cf for cf in self.cashflows if cf.date > reference_date]


class MarketSnapshot(BaseModel):
    model_config = ConfigDict(frozen=False)  # `instrument` se setea post-fetch

    instrument: Optional[Instrument] = None
    price: Optional[float] = None  # None cuando no hay snapshot live (bond_detail popup)
    last_update: Optional[_date] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: Optional[float] = None   # ARS notional traded today (Data912 "v")
    operations: Optional[int] = None  # number of trades today (Data912 "q_op")
    change_pct: Optional[float] = None


@dataclass
class InstrumentMetrics:
    snapshot: MarketSnapshot
    tir: Optional[float] = None
    duration: Optional[float] = None  # Modified Duration (convención BYMA/IAMC)
    technical_value: Optional[float] = None
    parity: Optional[float] = None
    variance_7d: Optional[float] = None
    variance_30d: Optional[float] = None
    variance_90d: Optional[float] = None
    variance_ytd: Optional[float] = None
    variance_1y: Optional[float] = None
