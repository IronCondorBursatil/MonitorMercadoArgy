"""ORM SQLAlchemy 2.0 (typed). Espejo 1:1 de los campos del dominio `Instrument`
+ `Cashflow`. Las columnas de cashflow usan los nombres del Excel (amortizacion,
cupon_interes, fecha_pago) para que el seeding sea directo.

`sheet` + `raw_fields` (JSON) preservan los parámetros crudos por hoja del ABM
(cupón %, tem_licit, schedule de amort, etc.) para que el form de edición pueda
hacer round-trip — el `Instrument` normalizado solo no alcanza para reconstruir
esos inputs (quedan horneados en los cashflows materializados)."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class UserORM(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String)
    is_admin: Mapped[bool] = mapped_column(default=False)
    allowed_tabs: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["*"])



class BymaCatalogORM(Base):
    """Universo de especies de BYMA (referencia navegable/buscable, NO el catálogo de
    pricing). Una fila por símbolo cotizante (~6.4k). Se llena del seed
    `data/byma/titulos_final.csv` (symbol→ISIN/categoría/emisor/...). Tabla derivada:
    se puede borrar y reingerir sin pérdida (≠ `instruments`, que es la verdad ABM)."""

    __tablename__ = "byma_catalog"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    ticker_pesos: Mapped[Optional[str]] = mapped_column(String, default=None)
    isin: Mapped[Optional[str]] = mapped_column(String, default=None)
    categoria: Mapped[Optional[str]] = mapped_column(String, default=None)   # Acciones/Cedears/ON/Tit.Públicos...
    moneda: Mapped[Optional[str]] = mapped_column(String, default=None)      # ARS | MEP | cable
    security_type: Mapped[Optional[str]] = mapped_column(String, default=None)  # CS|CD|CORP|GO|FUT
    clase_liquidacion: Mapped[Optional[str]] = mapped_column(String, default=None)
    cotiza: Mapped[Optional[int]] = mapped_column(default=None)
    segmento: Mapped[Optional[str]] = mapped_column(String, default=None)
    panel: Mapped[Optional[str]] = mapped_column(String, default=None)
    ins_type: Mapped[Optional[str]] = mapped_column(String, default=None)    # EQUITY | BOND
    emisor: Mapped[Optional[str]] = mapped_column(String, default=None)
    sector: Mapped[Optional[str]] = mapped_column(String, default=None)      # futuro (ficha sociedad)
    updated_at: Mapped[Optional[str]] = mapped_column(String, default=None)


# Índices para el buscador (ticker/ISIN/emisor/categoría).
Index("ix_byma_isin", BymaCatalogORM.isin)
Index("ix_byma_categoria", BymaCatalogORM.categoria)
Index("ix_byma_ticker_pesos", BymaCatalogORM.ticker_pesos)
Index("ix_byma_emisor", BymaCatalogORM.emisor)


class InstrumentORM(Base):
    __tablename__ = "instruments"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)  # primario (ARS/pesos o el principal)
    # Multi-ticker: mismas condiciones, distinta moneda de liquidación (sufijo
    # D=MEP, C=CABLE). Opcionales; al cargar se expande a una especie por ticker.
    ticker_mep: Mapped[Optional[str]] = mapped_column(String, default=None)
    ticker_ccl: Mapped[Optional[str]] = mapped_column(String, default=None)
    short_name: Mapped[str] = mapped_column(String, default="")
    instrument_type: Mapped[str] = mapped_column(String, default="")
    # ISIN (clave del activo en BYMA; mismo ISIN = mismo activo en sus monedas).
    # Lo llena el enriquecimiento BYMA y/o la ABM; display-only (el motor lo ignora).
    isin: Mapped[Optional[str]] = mapped_column(String, default=None)
    maturity_date: Mapped[Optional[date]] = mapped_column(default=None)
    emission_date: Mapped[Optional[date]] = mapped_column(default=None)
    cer_base: Mapped[Optional[float]] = mapped_column(default=None)
    cer_lag: Mapped[int] = mapped_column(default=10)
    category: Mapped[Optional[str]] = mapped_column(default=None)
    floor_rate_monthly: Mapped[Optional[float]] = mapped_column(default=None)
    spread_rate: Mapped[Optional[float]] = mapped_column(default=None)
    cer_spread: Mapped[Optional[float]] = mapped_column(default=None)
    payment_frequency: Mapped[int] = mapped_column(default=2)
    day_count: Mapped[str] = mapped_column(String, default="ACT/365.25")
    # Metadata del ABM (no la usa el motor de pricing).
    sheet: Mapped[Optional[str]] = mapped_column(String, default=None)
    raw_fields: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=None)

    cashflows: Mapped[List["CashflowORM"]] = relationship(
        back_populates="instrument",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="CashflowORM.fecha_pago",
    )


# Índices aditivos (forward-only): lookup por pata MEP/CABLE sin full-scan.
# create_all los crea en DBs nuevas; _migrate_table_add_columns los agrega
# a DBs existentes mediante CREATE INDEX IF NOT EXISTS en init_db.
Index("ix_instr_mep", InstrumentORM.ticker_mep)
Index("ix_instr_ccl", InstrumentORM.ticker_ccl)
Index("ix_instr_isin", InstrumentORM.isin)


class CashflowORM(Base):
    __tablename__ = "cashflows"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("instruments.ticker", ondelete="CASCADE"))
    fecha_pago: Mapped[date] = mapped_column()
    amortizacion: Mapped[float] = mapped_column(default=0.0)
    cupon_interes: Mapped[float] = mapped_column(default=0.0)

    instrument: Mapped["InstrumentORM"] = relationship(back_populates="cashflows")
