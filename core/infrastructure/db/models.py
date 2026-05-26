"""ORM SQLAlchemy 2.0 (typed). Espejo 1:1 de los campos del dominio `Instrument`
+ `Cashflow`. Las columnas de cashflow usan los nombres del Excel (amortizacion,
cupon_interes, fecha_pago) para que el seeding sea directo."""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class InstrumentORM(Base):
    __tablename__ = "instruments"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    short_name: Mapped[str] = mapped_column(String, default="")
    instrument_type: Mapped[str] = mapped_column(String, default="")
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

    cashflows: Mapped[List["CashflowORM"]] = relationship(
        back_populates="instrument",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="CashflowORM.fecha_pago",
    )


class CashflowORM(Base):
    __tablename__ = "cashflows"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("instruments.ticker", ondelete="CASCADE"))
    fecha_pago: Mapped[date] = mapped_column()
    amortizacion: Mapped[float] = mapped_column(default=0.0)
    cupon_interes: Mapped[float] = mapped_column(default=0.0)

    instrument: Mapped["InstrumentORM"] = relationship(back_populates="cashflows")
