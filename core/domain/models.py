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

from pydantic import BaseModel, ConfigDict

_UNSET = object()


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
        t = self.norm_type
        return "DOLAR_LINKED" in t or "DOLAR LINKED" in t

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

    def get_future_cashflows(self, reference_date: _date) -> List[Cashflow]:
        return [cf for cf in self.cashflows if cf.date >= reference_date]

    # Alias nuevo (nomenclatura del plan); mismo comportamiento.
    def future_cashflows(self, ref: _date) -> List[Cashflow]:
        return self.get_future_cashflows(ref)


class MarketSnapshot(BaseModel):
    model_config = ConfigDict(frozen=False)  # `instrument` se setea post-fetch

    instrument: Optional[Instrument] = None
    price: float = 0.0
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
    variance_1y: Optional[float] = None
