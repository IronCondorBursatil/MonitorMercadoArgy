"""Tests del parser de tickers BYMA y resolución de strikes."""
from __future__ import annotations

import pytest

from core.domain.options.symbols import parse_ticker, resolve_strike


@pytest.mark.parametrize("sym, root, kind, strike_str, month", [
    ("ALUC1000JU", "ALU", "C", "1000", 6),
    ("ALUV1050JU", "ALU", "V", "1050", 6),
    ("GFGC70553J", "GFG", "C", "70553", 6),
    ("GFGV62553J", "GFG", "V", "62553", 6),
    ("COMC33.0JU", "COM", "C", "33.0", 6),
    ("COMC35.0AG", "COM", "C", "35.0", 8),
    ("BYMC287.5J", "BYM", "C", "287.5", 6),
    ("BHIC320.AG", "BHI", "C", "320.", 8),
    ("PAMC5300JU", "PAM", "C", "5300", 6),
    ("CREV1700AG", "CRE", "V", "1700", 8),
])
def test_parse_real_tickers(sym, root, kind, strike_str, month):
    meta = parse_ticker(sym)
    assert meta is not None, f"{sym} should parse"
    assert meta.root == root
    assert meta.kind == kind
    assert meta.strike_str == strike_str
    assert meta.month == month


@pytest.mark.parametrize("bad", ["", "X", "ALUC100", "ALUZ1000JU", "AL1C1000JU", "1234567890"])
def test_parse_garbage(bad):
    assert parse_ticker(bad) is None


def test_call_put_helpers():
    c = parse_ticker("ALUC1000JU")
    v = parse_ticker("ALUV1000JU")
    assert c.is_call and not c.is_put
    assert v.is_put and not v.is_call


def test_lowercase_normalized():
    """El parser debe aceptar minúsculas (normaliza a mayúscula)."""
    assert parse_ticker("aluc1000ju") is not None


@pytest.mark.parametrize("strike_str, spot, expected", [
    ("1000", 994.5,  1000.0),     # entero razonable vs spot → tal cual
    ("70553", 7220, 7055.3),       # entero "10× muy alto" → divide /10
    ("33.0", 47.37, 33.0),         # decimal explícito → tal cual
    ("287.5", 295,  287.5),        # decimal explícito
    ("320.", 320,   320.0),        # punto de padding
])
def test_resolve_strike(strike_str, spot, expected):
    v = resolve_strike(strike_str, spot)
    assert v is not None
    assert abs(v - expected) < 0.01


def test_resolve_strike_no_spot():
    """Sin spot, no aplicamos heurística — se devuelve el entero."""
    v = resolve_strike("70553", None)
    assert v == 70553.0
