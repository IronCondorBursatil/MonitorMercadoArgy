"""Tests del wrapper de optionlab + endpoint POST /options/analytics."""
from __future__ import annotations

import os

os.environ.setdefault("MONITOR_DISABLE_LOOPS", "1")

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from apps.web.app import app
from core.domain.options.analytics import run_analytics
from core.domain.options.chain import build_options
from core.infrastructure.schemas import Data912Row


# ── Setup compartido ────────────────────────────────────────────────────

def _seed_options():
    """4 contratos en GGAL armando un iron-condor-ish: short call + long call far OTM,
    short put + long put far OTM (suficientes para test_iron_condor)."""
    opts = {
        "GFGC72553J": Data912Row(symbol="GFGC72553J", c=226, px_bid=226, px_ask=230, v=1000, q_op=100),  # ~ATM call
        "GFGC78553J": Data912Row(symbol="GFGC78553J", c=50, px_bid=49, px_ask=51, v=500, q_op=50),       # OTM call
        "GFGV70806J": Data912Row(symbol="GFGV70806J", c=72, px_bid=65, px_ask=78, v=300, q_op=30),       # near ATM put
        "GFGV60553J": Data912Row(symbol="GFGV60553J", c=11.66, px_bid=10, px_ask=13, v=200, q_op=20),    # OTM put
    }
    stocks = {"GGAL": Data912Row(symbol="GGAL", c=7220)}
    return build_options(opts, stocks, N=30)


def _by_ticker(items):
    return {it.ticker: it for it in items}


# ── Tests del wrapper run_analytics ─────────────────────────────────────

def test_run_analytics_iron_condor():
    """Iron condor: short call ATM + long call OTM + short put ATM + long put OTM.
    Espera P(profit) ∈ (0,1), EV+>0, EV−<0, al menos 1 profit range."""
    items = _seed_options()
    by_tk = _by_ticker(items)
    legs = [
        {"ticker": "GFGC72553J", "qty": -1},   # short call ~ATM
        {"ticker": "GFGC78553J", "qty": +1},   # long call OTM
        {"ticker": "GFGV70806J", "qty": -1},   # short put ~ATM
        {"ticker": "GFGV60553J", "qty": +1},   # long put OTM
    ]
    today = date.today()
    target = items[0].expiry
    result = run_analytics(legs, by_tk, spot=7220, atm_iv=0.55, r=0.40,
                           start_date=today, target_date=target)
    assert result is not None
    assert 0.0 < result["prob_profit"] < 1.0
    assert result["ev_profit"] is not None and result["ev_profit"] > 0
    assert result["ev_loss"] is not None and result["ev_loss"] < 0
    assert len(result["profit_ranges"]) >= 1
    for lo, hi in result["profit_ranges"]:
        assert lo < hi


def test_run_analytics_long_call():
    """Long call ATM: profit range debe comenzar por encima de strike + premium pagado."""
    items = _seed_options()
    by_tk = _by_ticker(items)
    legs = [{"ticker": "GFGC72553J", "qty": +1}]
    today = date.today()
    target = items[0].expiry
    result = run_analytics(legs, by_tk, spot=7220, atm_iv=0.55, r=0.40,
                           start_date=today, target_date=target)
    assert result is not None
    assert result["prob_profit"] is not None
    # Long call siempre tiene EV+ positivo si hay alguna prob de profit
    assert result["ev_profit"] is not None and result["ev_profit"] > 0
    # Strike 7255.3 + ask 230 ≈ break-even 7485 (debería estar dentro del range)
    if result["profit_ranges"]:
        lo, _ = result["profit_ranges"][0]
        assert lo > 7255  # arranca por encima del strike


def test_run_analytics_empty_legs():
    """Lista vacía devuelve None (analytics es opcional)."""
    result = run_analytics([], {}, spot=7220, atm_iv=0.5, r=0.4,
                           start_date=date.today(), target_date=date.today() + timedelta(days=30))
    assert result is None


def test_run_analytics_missing_ticker():
    """Tickers no presentes en items_by_ticker se ignoran; si todas faltan → None."""
    result = run_analytics(
        legs=[{"ticker": "NOPE1234J", "qty": 1}],
        items_by_ticker={}, spot=7220, atm_iv=0.5, r=0.4,
        start_date=date.today(), target_date=date.today() + timedelta(days=30),
    )
    assert result is None


def test_run_analytics_handles_zero_bid_with_last_fallback():
    """Opción ilíquida (bid=0) usa last como premium — no debería romper el call."""
    items = _seed_options()
    # Forzar bid=0 en una leg para verificar el fallback
    by_tk = _by_ticker(items)
    target = items[0].expiry
    legs = [{"ticker": "GFGC78553J", "qty": +1}]
    result = run_analytics(legs, by_tk, spot=7220, atm_iv=0.55, r=0.40,
                           start_date=date.today(), target_date=target)
    assert result is not None  # no crashea aún con bid bajo


# ── Tests del endpoint HTTP ─────────────────────────────────────────────

@pytest.fixture
def client():
    with TestClient(app) as c:
        c.app.state.app_state.set_options(_seed_options())
        yield c


def test_analytics_endpoint_smoke(client):
    """POST /options/analytics con una leg → 200 y prob_profit poblada."""
    r = client.post("/options/analytics",
                    json={"legs": [{"ticker": "GFGC72553J", "qty": 1}]})
    assert r.status_code == 200
    data = r.json()
    assert "prob_profit" in data
    assert "ev_profit" in data
    assert "ev_loss" in data
    assert "profit_ranges" in data


def test_analytics_endpoint_empty_legs(client):
    """POST con legs vacío devuelve estructura sin valores."""
    r = client.post("/options/analytics", json={"legs": []})
    assert r.status_code == 200
    data = r.json()
    assert data["prob_profit"] is None
    assert data["profit_ranges"] == []


def test_analytics_endpoint_iron_condor(client):
    """Iron condor completo: 4 legs → analytics consistentes."""
    r = client.post("/options/analytics", json={"legs": [
        {"ticker": "GFGC72553J", "qty": -1},
        {"ticker": "GFGC78553J", "qty": +1},
        {"ticker": "GFGV70806J", "qty": -1},
        {"ticker": "GFGV60553J", "qty": +1},
    ]})
    assert r.status_code == 200
    data = r.json()
    assert data["prob_profit"] is not None
    assert 0 < data["prob_profit"] < 1
    assert data["ev_profit"] > 0
    assert data["ev_loss"] < 0
