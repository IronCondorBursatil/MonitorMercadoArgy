"""Mapeo puro fila BYMA → Data912Row (sin red)."""

from core.infrastructure.byma.field_map import byma_row_to_quote, settle_of


def test_non_finite_prices_dropped_or_nulled():
    """A5: BYMA puede enviar NaN/Inf. `trade` no finito cae al fallback de cierre;
    bid/ask/volume no finitos se normalizan a None (no fluyen al motor/store)."""
    # trade=NaN, closingPrice=Inf → ambos no finitos → cae a previousClosingPrice 97.0
    q = byma_row_to_quote({"symbol": "AL30", "trade": float("nan"),
                           "closingPrice": float("inf"), "previousClosingPrice": 97.0})
    assert q is not None and q.c == 97.0
    # bid/volume no finitos → None (el precio es válido, la fila sobrevive)
    q2 = byma_row_to_quote({"symbol": "AL30", "trade": 100.0,
                            "bidPrice": float("nan"), "volumeAmount": float("inf")})
    assert q2 is not None and q2.c == 100.0
    assert q2.px_bid is None and q2.v is None


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


def test_no_trade_falls_back_to_closing_price():
    """Sin operar en la rueda (trade 0/None) → c = CIERRE (closingPrice), para que la
    especie igual aparezca con precio de mercado (todo el universo de ONs)."""
    q = byma_row_to_quote({"symbol": "YMCXO", "trade": 0, "closingPrice": 98.5,
                           "previousClosingPrice": 97.0})
    assert q.c == 98.5
    q2 = byma_row_to_quote({"symbol": "YMCXO", "trade": None, "closingPrice": None,
                            "previousClosingPrice": 97.0})
    assert q2.c == 97.0                      # cae al cierre anterior si no hay cierre del día


def test_last_trade_wins_over_close():
    """Si operó, manda el LAST (trade), no el cierre."""
    q = byma_row_to_quote({"symbol": "YMCXO", "trade": 99.9, "closingPrice": 98.5})
    assert q.c == 99.9


def test_truly_no_price_stays_zero():
    """Sin LAST ni cierre ni cierre anterior → 0.0 (queda oculta donde se exige precio)."""
    q = byma_row_to_quote({"symbol": "ZZZ", "trade": 0, "closingPrice": 0,
                           "previousClosingPrice": None})
    assert q.c == 0.0


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
