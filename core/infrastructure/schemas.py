"""Validación Pydantic en el borde de ingesta (deuda #3): una fila corrupta de
Data912 no debe tumbar el batch entero. `parse_snapshot_rows` valida fila por
fila y descarta las inválidas."""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


class Data912Row(BaseModel):
    symbol: str
    c: float = Field(ge=0)               # last price
    px_bid: Optional[float] = None
    px_ask: Optional[float] = None
    v: Optional[float] = None            # ARS notional traded today
    q_op: Optional[int] = None           # number of trades today
    pct_change: Optional[float] = None
    # --- Metadata de opciones (solo el panel BYMA /options la trae; None en el
    # resto). Permite a la chain usar datos AUTORITATIVOS en vez de derivar todo
    # del ticker: `oi` = open interest REAL (no el q_op=nº de trades), y el
    # subyacente/tipo/vto vienen de la API → contratos con root no mapeado en
    # roots.py sobreviven (más profundidad). Data912 deja estos campos en None
    # → la chain cae al parser de ticker (comportamiento previo). ---
    oi: Optional[float] = None           # openInterest (interés abierto real)
    opt_kind: Optional[str] = None       # "C" (call) | "V" (put), de optionType
    opt_underlying: Optional[str] = None # underlyingSymbol (ticker del subyacente)
    opt_expiry: Optional[str] = None     # maturityDate ISO 'YYYY-MM-DD'

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, v: str) -> str:
        return v.upper().strip()


def parse_snapshot_rows(payload: list) -> "dict[str, Data912Row]":
    """Valida un payload crudo de Data912 → {symbol: Data912Row}. Filas inválidas
    se descartan (no tumban el batch)."""
    out: dict = {}
    for raw in payload:
        try:
            row = Data912Row.model_validate(raw)
        except Exception:
            continue
        out[row.symbol] = row
    return out
