"""Tests de cartera_store: persistencia de tenencias del usuario (M0.3).

Es dato del usuario (no recuperable del Excel) y no tenía ni una aserción. Cubre
CRUD, normalización, validación, persistencia atómica y tolerancia a JSON corrupto.
`_PATH` se redirige a tmp para no tocar la cartera real (data/cartera.json)."""

from __future__ import annotations

import json

import pytest

from apps.web import cartera_store


@pytest.fixture(autouse=True)
def _isolate_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cartera_store, "_PATH", str(tmp_path / "cartera.json"))
    return tmp_path


def test_upsert_creates_then_lists():
    res = cartera_store.upsert_holding("AL30", 1000, cost_price=72.5, note="core")
    assert res == {"action": "created", "ticker": "AL30"}
    holdings = cartera_store.list_holdings()
    assert len(holdings) == 1
    h = holdings[0]
    assert h["ticker"] == "AL30" and h["nominal"] == 1000.0
    assert h["cost_price"] == 72.5 and h["note"] == "core"


def test_upsert_updates_existing_and_preserves_added():
    cartera_store.upsert_holding("gd30", 500)            # minúscula → normaliza
    first_added = cartera_store.list_holdings()[0]["added"]
    res = cartera_store.upsert_holding("GD30", 800, cost_price=60.0)
    assert res["action"] == "updated"
    holdings = cartera_store.list_holdings()
    assert len(holdings) == 1, "update no debe duplicar"
    assert holdings[0]["nominal"] == 800.0
    assert holdings[0]["added"] == first_added, "added original debe preservarse"


def test_ticker_is_normalized_uppercase_trimmed():
    cartera_store.upsert_holding("  al30  ", 1)
    assert cartera_store.list_holdings()[0]["ticker"] == "AL30"


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_upsert_rejects_empty_ticker(bad):
    with pytest.raises(ValueError):
        cartera_store.upsert_holding(bad, 100)


@pytest.mark.parametrize("bad", ["abc", None])
def test_upsert_rejects_non_numeric_nominal(bad):
    with pytest.raises(ValueError):
        cartera_store.upsert_holding("AL30", bad)


@pytest.mark.parametrize("bad", [0, -5])
def test_upsert_rejects_non_positive_nominal(bad):
    with pytest.raises(ValueError):
        cartera_store.upsert_holding("AL30", bad)


def test_upsert_rejects_bad_cost_price():
    with pytest.raises(ValueError):
        cartera_store.upsert_holding("AL30", 100, cost_price="not-a-number")


def test_cost_price_empty_string_becomes_none():
    cartera_store.upsert_holding("AL30", 100, cost_price="")
    assert cartera_store.list_holdings()[0]["cost_price"] is None


def test_delete_removes_holding():
    cartera_store.upsert_holding("AL30", 100)
    cartera_store.upsert_holding("GD30", 200)
    res = cartera_store.delete_holding("al30")
    assert res == {"action": "deleted", "ticker": "AL30"}
    tickers = {h["ticker"] for h in cartera_store.list_holdings()}
    assert tickers == {"GD30"}


def test_delete_nonexistent_is_not_found():
    res = cartera_store.delete_holding("ZZZZ")
    assert res == {"action": "not_found", "ticker": "ZZZZ"}


def test_persisted_file_shape_and_atomic_roundtrip(_isolate_path):
    cartera_store.upsert_holding("AL30", 100)
    path = _isolate_path / "cartera.json"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"holdings", "updated"}
    assert isinstance(data["holdings"], list) and len(data["holdings"]) == 1
    # no debe quedar un .tmp colgado tras el os.replace
    assert not (_isolate_path / "cartera.json.tmp").exists()


def test_corrupt_json_degrades_to_empty(_isolate_path):
    (_isolate_path / "cartera.json").write_text("{ truncated", encoding="utf-8")
    assert cartera_store.list_holdings() == []  # no crash
    # y se puede seguir operando sobre la base vacía
    cartera_store.upsert_holding("AL30", 1)
    assert len(cartera_store.list_holdings()) == 1


def test_read_supports_legacy_bare_list(_isolate_path):
    """Tolera el formato viejo (lista pelada, sin el wrapper {holdings})."""
    (_isolate_path / "cartera.json").write_text(
        json.dumps([{"ticker": "AL30", "nominal": 100}]), encoding="utf-8")
    holdings = cartera_store.list_holdings()
    assert len(holdings) == 1 and holdings[0]["ticker"] == "AL30"
