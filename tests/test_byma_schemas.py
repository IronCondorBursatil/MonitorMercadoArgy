"""A5 — Data912Row rechaza precios no finitos (NaN/Inf) y normaliza bid/ask/v
no finitos a None, sin tumbar el batch (validación fila por fila)."""

import math

import pytest
from pydantic import ValidationError

from core.infrastructure.schemas import Data912Row, parse_snapshot_rows


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_data912row_rejects_non_finite_c(bad):
    """`c` (precio → XIRR/MD) no finito levanta ValidationError → la fila se descarta."""
    with pytest.raises(ValidationError):
        Data912Row(symbol="AL30", c=bad)


def test_parse_snapshot_rows_drops_nan_keeps_batch():
    """Una fila con c=NaN se descarta; el resto del batch sobrevive (no tumba todo)."""
    out = parse_snapshot_rows([
        {"symbol": "AL30", "c": float("nan")},
        {"symbol": "GD30", "c": 100.0},
    ])
    assert "AL30" not in out
    assert "GD30" in out and out["GD30"].c == 100.0


def test_data912row_normalizes_non_finite_bid_ask_volume():
    """NaN/Inf en bid/ask/volumen NO descartan la fila (el precio puede ser válido):
    se normalizan a None en vez de fluir al MarketSnapshot/UI."""
    row = Data912Row(symbol="X", c=100.0, px_bid=float("nan"),
                     px_ask=float("inf"), v=float("-inf"))
    assert row.c == 100.0
    assert row.px_bid is None and row.px_ask is None and row.v is None


def test_data912row_finite_bid_ask_volume_survive():
    row = Data912Row(symbol="X", c=100.0, px_bid=99.0, px_ask=101.0, v=5000.0)
    assert row.px_bid == 99.0 and row.px_ask == 101.0 and row.v == 5000.0
    assert all(math.isfinite(x) for x in (row.px_bid, row.px_ask, row.v))
