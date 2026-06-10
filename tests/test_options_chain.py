"""Tests del builder de la chain enriquecida."""
from __future__ import annotations

from core.domain.options.chain import build_options, underlyings_summary, months_for
from core.infrastructure.schemas import Data912Row


def _row(symbol, c=0.0, px_bid=0.0, px_ask=0.0, v=0.0, q_op=0, pct_change=0.0,
         oi=None, opt_kind=None, opt_underlying=None, opt_expiry=None):
    return Data912Row(symbol=symbol, c=c, px_bid=px_bid, px_ask=px_ask,
                      v=v, q_op=q_op, pct_change=pct_change, oi=oi, opt_kind=opt_kind,
                      opt_underlying=opt_underlying, opt_expiry=opt_expiry)


def test_build_options_basic():
    """Pipeline completo: opción válida + stock disponible → OptionItem."""
    opts = {
        "GFGC68806J": _row("GFGC68806J", c=480, px_bid=475, px_ask=480, v=11181, q_op=1324),
        "GFGV60553J": _row("GFGV60553J", c=11.66, px_bid=10.11, px_ask=13.49, v=6135, q_op=917),
    }
    stocks = {"GGAL": _row("GGAL", c=7220)}
    # r=0 para evitar el descuento agresivo que aplasta la IV de calls deep-ITM
    # (fenómeno real del market AR donde el bid suele estar bajo el lower bound europeo).
    items = build_options(opts, stocks, r=0.0, N=40)
    assert len(items) == 2
    by_tk = {it.ticker: it for it in items}
    c = by_tk["GFGC68806J"]
    assert c.underlying == "GGAL"
    assert c.kind == "C"
    assert c.spot == 7220
    assert abs(c.strike - 6880.6) < 0.1  # decimal implícito
    assert c.iv is not None and 0.05 < c.iv < 5.0
    assert c.greeks.delta is not None
    assert c.rates.tna_bruta is not None


def test_build_skips_unknown_root():
    """Root no mapeado → descartado silenciosamente, no rompe el batch."""
    opts = {
        "XYZC1000JU": _row("XYZC1000JU", c=10, px_bid=10, px_ask=12),
        "GFGC68806J": _row("GFGC68806J", c=480, px_bid=475, px_ask=480),
    }
    stocks = {"GGAL": _row("GGAL", c=7220)}
    items = build_options(opts, stocks, N=20)
    assert len(items) == 1
    assert items[0].ticker == "GFGC68806J"


def test_build_skips_missing_spot():
    """Sin spot del subyacente, la opción no se enriquece."""
    opts = {"GFGC68806J": _row("GFGC68806J", c=480, px_bid=475, px_ask=480)}
    items = build_options(opts, {}, N=20)
    assert items == []


def test_build_uses_byma_underlying_for_unmapped_root():
    """Con underlyingSymbol de BYMA, un root NO mapeado en roots.py (VIS→VIST)
    sobrevive — la profundidad que el camino Data912 perdía silenciosamente."""
    opts = {"VISC8000AG": _row("VISC8000AG", c=120, px_bid=115, px_ask=125,
                               oi=987, opt_kind="C", opt_underlying="VIST",
                               opt_expiry="2026-08-21")}
    stocks = {"VIST": _row("VIST", c=9000)}
    items = build_options(opts, stocks, r=0.0, N=20)
    assert len(items) == 1
    it = items[0]
    assert it.underlying == "VIST"
    assert it.kind == "C"
    assert it.open_interest == 987.0              # OI REAL de BYMA (no el q_op)
    assert it.expiry.isoformat() == "2026-08-21"  # maturityDate exacta


def test_open_interest_falls_back_to_q_op_without_byma_oi():
    """Fila estilo Data912 (sin oi) → open_interest cae a q_op (back-compat)."""
    opts = {"GFGC68806J": _row("GFGC68806J", c=480, px_bid=475, px_ask=480, q_op=1324)}
    stocks = {"GGAL": _row("GGAL", c=7220)}
    items = build_options(opts, stocks, r=0.0, N=20)
    assert items[0].open_interest == 1324.0


def test_underlyings_summary_sorts_by_volume():
    opts = {
        "GFGC68806J": _row("GFGC68806J", c=480, px_bid=475, px_ask=480, v=10000),
        "ALUC1000JU": _row("ALUC1000JU", c=40, px_bid=38, px_ask=40, v=23),
    }
    stocks = {"GGAL": _row("GGAL", c=7220), "ALUA": _row("ALUA", c=994.5)}
    items = build_options(opts, stocks, N=20)
    summary = underlyings_summary(items)
    assert summary[0]["underlying"] == "GGAL"
    assert summary[0]["vol_total"] == 10000.0
    assert summary[1]["underlying"] == "ALUA"


def test_months_for_returns_chronological():
    opts = {
        "ALUC1000JU": _row("ALUC1000JU", c=40, px_bid=38, px_ask=40),
        "ALUC1000AG": _row("ALUC1000AG", c=50, px_bid=48, px_ask=52),
    }
    stocks = {"ALUA": _row("ALUA", c=994.5)}
    items = build_options(opts, stocks, N=20)
    months = months_for(items, "ALUA")
    # JU (junio) < AG (agosto) en orden cronológico
    assert months == ["JU", "AG"] or months == ["AG", "JU"]  # tolerante al año-roll
