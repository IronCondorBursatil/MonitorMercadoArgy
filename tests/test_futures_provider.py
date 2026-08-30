"""Tests de core/infrastructure/futures_provider.py (M3a) — partes puras.

Se testea SIN tocar el WebSocket: parsing de contratos, TEA implícita, resolución
de spot (híbrido horario, con providers stub) y el parser de mensajes M: del feed
Primary. El cliente WS (thread daemon) queda fuera del alcance unitario."""

from __future__ import annotations

from datetime import date

import pytest

from core.infrastructure.futures_provider import (
    DEFAULT_SYMBOLS,
    implied_tea,
    parse_contract_maturity,
    resolve_spot_for_tna,
    _parse_m_message,
    _ticker_to_security_id,
)


# --- parse_contract_maturity ------------------------------------------------ #

def test_parse_contract_maturity_last_day_of_month():
    assert parse_contract_maturity("DLR/MAY26") == date(2026, 5, 31)
    assert parse_contract_maturity("DLR/FEB26") == date(2026, 2, 28)   # no bisiesto
    assert parse_contract_maturity("DLR/ENE27") == date(2027, 1, 31)


def test_parse_contract_maturity_strips_trailing_m():
    assert parse_contract_maturity("DLR/MAY26M") == date(2026, 5, 31)


@pytest.mark.parametrize("bad", ["NOSLASH", "DLR/XXX26", "DLR/MA26", "DLR/MAYAA", "DLR/"])
def test_parse_contract_maturity_invalid_is_none(bad):
    assert parse_contract_maturity(bad) is None


def test_default_symbols_all_parse():
    for sym in DEFAULT_SYMBOLS:
        assert parse_contract_maturity(sym) is not None


# --- _ticker_to_security_id ------------------------------------------------- #

def test_ticker_to_security_id():
    assert _ticker_to_security_id("DLR/MAY26") == "rx_DDF_DLR_MAY26"
    assert _ticker_to_security_id("DLR/SPOT") == "rx_DDF_BCRA_A3500"
    assert _ticker_to_security_id("NOSLASH") is None


# --- implied_tea (efectiva anual compuesta) --------------------------------- #

def test_implied_tea_golden():
    # futuro 1100, spot 1000, 365 días → (1.1)^(365/365)-1 = 0.10
    tea = implied_tea(1100.0, 1000.0, date(2027, 1, 1), today=date(2026, 1, 1))
    assert tea == pytest.approx(0.10, abs=1e-9)


def test_implied_tea_half_year_annualizes():
    # +5% en ~182.5 días → anualizado (compuesto) ≈ (1.05)^2-1 = 0.1025
    tea = implied_tea(1050.0, 1000.0, date(2026, 7, 2), today=date(2026, 1, 1))
    assert tea == pytest.approx((1.05) ** (365 / 182) - 1, abs=1e-6)


def test_implied_tea_guards():
    assert implied_tea(0, 1000, date(2027, 1, 1), today=date(2026, 1, 1)) is None
    assert implied_tea(1100, 0, date(2027, 1, 1), today=date(2026, 1, 1)) is None
    # vencimiento en el pasado → None
    assert implied_tea(1100, 1000, date(2025, 1, 1), today=date(2026, 1, 1)) is None


# --- resolve_spot_for_tna (con providers stub) ------------------------------ #

class _Fx:
    def __init__(self, mid): self._mid = mid
    def get_mayorista_mid(self): return self._mid


class _Idx:
    def __init__(self, a3500): self._a = a3500
    def get_a3500(self): return self._a


def test_resolve_spot_prefers_a3500_off_hours(monkeypatch):
    monkeypatch.setattr("core.infrastructure.futures_provider._is_market_hours", lambda: False)
    assert resolve_spot_for_tna(_Fx(1105.0), _Idx(1100.0)) == 1100.0   # A3500 primero


def test_resolve_spot_falls_back_to_fx_off_hours(monkeypatch):
    monkeypatch.setattr("core.infrastructure.futures_provider._is_market_hours", lambda: False)
    assert resolve_spot_for_tna(_Fx(1105.0), _Idx(None)) == 1105.0     # A3500 no disp → fx


def test_resolve_spot_prefers_fx_during_market(monkeypatch):
    monkeypatch.setattr("core.infrastructure.futures_provider._is_market_hours", lambda: True)
    assert resolve_spot_for_tna(_Fx(1105.0), _Idx(1100.0)) == 1105.0   # mid live


def test_resolve_spot_none_when_all_fail(monkeypatch):
    monkeypatch.setattr("core.infrastructure.futures_provider._is_market_hours", lambda: False)
    assert resolve_spot_for_tna(_Fx(None), _Idx(None)) is None


# --- _parse_m_message ------------------------------------------------------- #

def test_parse_m_message_extracts_fields():
    # 21 campos por posición (ver el mapa documentado en el módulo).
    parts = ["M:rx_DDF_DLR_MAY26", "1", "10", "1200.5", "1201.0", "12",
             "1200.8", "2026-06-10T12:00", "50", "60000000", "50000",
             "1199.0", "1205.0", "1200.0", "345", "1200.7", "2026-06-10",
             "1198.0", "2026-06-09", "1197.0", "2026-06-08"]
    msg = "|".join(parts)
    res = _parse_m_message(msg)
    assert res is not None
    sec, q = res
    assert sec == "rx_DDF_DLR_MAY26"
    assert q["bid"] == 1200.5 and q["ask"] == 1201.0
    assert q["last"] == 1200.8 and q["settle"] == 1200.7
    assert q["open_interest"] == 345.0
    assert q["day_high"] == 1205.0 and q["day_low"] == 1199.0
    assert q["prev_settle"] == 1198.0


def test_parse_m_message_rejects_non_m_and_short():
    assert _parse_m_message("X:foo|bar") is None
    assert _parse_m_message("M:short|1|2") is None   # < 16 campos
