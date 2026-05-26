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
