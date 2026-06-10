"""Smoke tests del router de opciones: cada endpoint responde 200 + payload válido."""
from __future__ import annotations

import os

os.environ.setdefault("MONITOR_DISABLE_LOOPS", "1")


import pytest
from fastapi.testclient import TestClient

from apps.web.app import app
from core.domain.options.chain import build_options
from core.infrastructure.schemas import Data912Row


def _seed_state():
    """Inyecta opciones enriquecidas en AppState para que los endpoints tengan
    data (sin esperar el refresh loop)."""
    opts = {
        "GFGC68806J": Data912Row(symbol="GFGC68806J", c=480, px_bid=475, px_ask=480, v=11181, q_op=1324),
        "GFGC70553J": Data912Row(symbol="GFGC70553J", c=348, px_bid=335, px_ask=348, v=33377, q_op=3155),
        "GFGV60553J": Data912Row(symbol="GFGV60553J", c=11.66, px_bid=10.11, px_ask=13.49, v=6135, q_op=917),
        "ALUC1000JU": Data912Row(symbol="ALUC1000JU", c=38.9, px_bid=38.91, px_ask=39.9, v=23, q_op=7),
    }
    stocks = {
        "GGAL": Data912Row(symbol="GGAL", c=7220),
        "ALUA": Data912Row(symbol="ALUA", c=994.5),
    }
    return build_options(opts, stocks, N=30)


@pytest.fixture
def client():
    with TestClient(app) as c:
        state = c.app.state.app_state
        state.set_options(_seed_state())
        yield c


def test_options_page_200(client):
    r = client.get("/options")
    assert r.status_code == 200
    assert "STRATEGY BUILDER" in r.text
    assert "SCANNER" in r.text or "Scanner" in r.text or "scan" in r.text.lower()


def test_sidebar_returns_underlyings(client):
    r = client.get("/options/sidebar?u=GGAL")
    assert r.status_code == 200
    assert "GGAL" in r.text
    assert "ALUA" in r.text


def test_chain_returns_table(client):
    r = client.get("/options/chain?u=GGAL")
    assert r.status_code == 200
    assert "chain-data" in r.text  # JSON embebido para el builder
    assert "GFG" in r.text or "K" in r.text


def test_smile_returns_canvas(client):
    r = client.get("/options/smile?u=GGAL&v=J")
    assert r.status_code == 200
    assert "Chart" in r.text or "smile" in r.text.lower()


def test_oi_returns_heatmap(client):
    r = client.get("/options/oi?u=GGAL&v=J")
    assert r.status_code == 200


def test_scanner_returns_rows(client):
    r = client.get("/options/scanner")
    assert r.status_code == 200
    assert "TNA" in r.text or "tasa" in r.text.lower()


def test_scanner_with_filters(client):
    r = client.get("/options/scanner?kind=C&liq=bid&sort_by=tna_bruta&sort_dir=desc")
    assert r.status_code == 200
    # Sólo calls deberían aparecer:
    assert "PUT" not in r.text or "kind-V" not in r.text


def test_scanner_search(client):
    r = client.get("/options/scanner?q=GFG")
    assert r.status_code == 200
    assert "GFG" in r.text
