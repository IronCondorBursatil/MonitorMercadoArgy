"""Mapeo puro fila BYMA → Data912Row (sin red)."""

from core.infrastructure.byma.field_map import byma_row_to_quote, settle_of


def test_maps_core_fields():
    q = byma_row_to_quote({
        "symbol": "al30", "trade": 123.45, "bidPrice": 123.0, "offerPrice": 124.0,
        "volumeAmount": 1000.0, "numberOfOrders": 7, "imbalance": -0.0356,
        "settlementType": 2,
    })
    assert q is not None
    assert q.symbol == "AL30"          # validator uppercases
    assert q.c == 123.45               # trade → c
    assert q.px_bid == 123.0
    assert q.px_ask == 124.0
    assert q.v == 1000.0               # volumeAmount → v
    assert q.q_op == 7                 # numberOfOrders → q_op
    assert abs(q.pct_change - (-3.56)) < 1e-9  # imbalance(fracción) → %


def test_number_of_orders_float_coerced_to_int():
    q = byma_row_to_quote({"symbol": "GGAL", "trade": 1.0, "numberOfOrders": 42.0})
    assert q.q_op == 42


def test_market_closed_zero_trade_ok():
    q = byma_row_to_quote({"symbol": "AL30", "trade": 0, "imbalance": None})
    assert q is not None
    assert q.c == 0.0
    assert q.pct_change is None


def test_no_symbol_returns_none():
    assert byma_row_to_quote({"trade": 1.0}) is None
    assert byma_row_to_quote({"symbol": ""}) is None
    assert byma_row_to_quote("not a dict") is None


def test_maps_option_fields():
    """Panel /options: openInterest/optionType/underlyingSymbol/maturityDate → metadata."""
    q = byma_row_to_quote({
        "symbol": "VISC8000AG", "trade": 12.0, "bidPrice": 11.0, "offerPrice": 13.0,
        "openInterest": 345.0, "optionType": "CALL", "underlyingSymbol": "vist",
        "maturityDate": "2026-08-21", "settlementType": 2,
    })
    assert q is not None
    assert q.oi == 345.0
    assert q.opt_kind == "C"
    assert q.opt_underlying == "VIST"      # uppercased
    assert q.opt_expiry == "2026-08-21"


def test_put_option_type_maps_to_v():
    q = byma_row_to_quote({"symbol": "GFGV70000JU", "trade": 5.0, "optionType": "PUT",
                           "underlyingSymbol": "GGAL"})
    assert q.opt_kind == "V"


def test_non_option_row_has_no_option_metadata():
    """Una fila de bono/acción no trae los campos de opción → todos None."""
    q = byma_row_to_quote({"symbol": "AL30", "trade": 100.0, "settlementType": 2})
    assert q.oi is None
    assert q.opt_kind is None
    assert q.opt_underlying is None
    assert q.opt_expiry is None


def test_settle_of():
    assert settle_of({"settlementType": 1}) == "CI"
    assert settle_of({"settlementType": "1"}) == "CI"
    assert settle_of({"settlementType": 2}) == "24"
    assert settle_of({"settlementType": "2"}) == "24"
    assert settle_of({}) == "24"          # sin dato → 24hs
    assert settle_of(None) == "24"
