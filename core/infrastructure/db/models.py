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
    # Version de los tokens del usuario. Va DENTRO del JWT: si no coincide con la de
    # la fila, el token no vale. Es la unica forma de revocar una sesion en un esquema
    # sin estado — hasta ahora, resetearle la contrasena a alguien (el gesto que uno
    # hace justo cuando sospecha que le entraron) NO lo sacaba: el JWT robado seguia
    # valiendo hasta que expirara. Forward-only: `init_db` la agrega con ALTER y las
    # filas viejas quedan en 0.
    token_version: Mapped[int] = mapped_column(default=0)



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
    """Una fila = un evento de pago per-100-VN, en términos BASE (ver agents.md).

    `es_ancla` marca una fila que NO es un pago: es el **vencimiento declarado** de un
    instrumento cuyo payoff es de FÓRMULA CERRADA (TAMAR PURO / DUAL / DUAL_CER_TAMAR
    — ver `instrument_groups.ANALYTIC_PAYOFF_TYPES`). Esos bonos no tienen —ni pueden
    tener— schedule nominal: su pago sale de `tamar.tamar_dual_payoff_at` sobre la
    TAMAR observada+proyectada, así que materializar un schedule sería *incorrecto*.
    Sin ninguna fila, en cambio, quedan indistinguibles de un bono a medio cargar y son
    invisibles en `/cashflows`.

    El ancla resuelve las dos cosas: existe en la DB (auditable, visible) y
    `catalog_repository._orm_to_domain` la FILTRA — nunca entra al dominio. Por eso la
    marca vive acá y no en `core.domain.models.Cashflow`: cero superficie nueva en el
    hot-path de pricing, que sigue viendo `cashflows=()` para esos 14 bonos. Cambiarla
    de lado (marcarla en el dominio) reabriría el riesgo que este diseño cierra."""

    __tablename__ = "cashflows"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("instruments.ticker", ondelete="CASCADE"))
    fecha_pago: Mapped[date] = mapped_column()
    amortizacion: Mapped[float] = mapped_column(default=0.0)
    cupon_interes: Mapped[float] = mapped_column(default=0.0)
    # Forward-only: `init_db` la agrega con ALTER ADD COLUMN sobre las DBs existentes
    # (default 0 = flujo real), nunca dropea. Ver `_migrate_table_add_columns`.
    es_ancla: Mapped[bool] = mapped_column(default=False)

    instrument: Mapped["InstrumentORM"] = relationship(back_populates="cashflows")
