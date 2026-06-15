"""Store de cierres diarios (price_history.py): upsert/get, acumulación batch,
priming desde Data912, y el merge CSV+store en el provider."""

import os
from datetime import date

import pytest

from core.infrastructure.price_history import (
    PriceHistoryStore, byma_prime_candidates, prime_from_byma_historico,
    prime_from_data912, record_live_closes,
)


@pytest.fixture
def store(tmp_path):
    return PriceHistoryStore(os.path.join(str(tmp_path), "ph.db"))


def test_upsert_and_get_series(store):
    n = store.upsert("AL30D", {date(2026, 1, 2): 63.5, date(2026, 1, 3): 64.0})
    assert n == 2
    s = store.get_series("al30d")          # case-insensitive
    assert s == {date(2026, 1, 2): 63.5, date(2026, 1, 3): 64.0}
    assert store.get_series("NOPE") == {}


def test_upsert_is_idempotent_and_updates(store):
    store.upsert("GD46D", {date(2026, 1, 2): 71.0})
    store.upsert("GD46D", {date(2026, 1, 2): 72.5})   # mismo día → overwrite
    assert store.get_series("GD46D") == {date(2026, 1, 2): 72.5}


def test_skips_nonpositive_closes(store):
    assert store.upsert("X", {date(2026, 1, 2): 0.0, date(2026, 1, 3): -1.0,
                              date(2026, 1, 4): 5.0}) == 1
    assert store.get_series("X") == {date(2026, 1, 4): 5.0}


def test_persists_across_instances(store, tmp_path):
    store.upsert("AE38D", {date(2026, 1, 2): 80.0})
    reopened = PriceHistoryStore(store._db_path)
    assert reopened.get_series("AE38D") == {date(2026, 1, 2): 80.0}


def test_record_live_closes_from_snapshot(store):
    class _Row:
        def __init__(self, c): self.c = c
    snap = {"AL30D": _Row(63.5), "GD46D": _Row(73.0),
            "BAD": _Row(0.0), "NONE": _Row(None)}
    n = record_live_closes(snap, store, date(2026, 6, 3))
    assert n == 2
    assert store.get_series("AL30D") == {date(2026, 6, 3): 63.5}
    assert store.get_series("BAD") == {}


def test_close_only_write_updates_close(store):
    """Un write close-only (sin volumes) actualiza el close; COALESCE SQL lo preserva."""
    from datetime import timedelta
    d = date.today() - timedelta(days=1)
    store.record_closes({"AL30D": 63.0}, d, volumes={"AL30D": 5000.0})
    store.record_closes({"AL30D": 64.0}, d)             # close-only → preserva volumen, actualiza close
    assert store.get_series("AL30D") == {d: 64.0}


def test_record_live_closes_writes_valid_rows(store):
    """record_live_closes escribe closes para filas con precio > 0 (independiente del volumen)."""
    class _Row:
        def __init__(self, c, v=None):
            self.c, self.v = c, v
    snap = {"TLCPD": _Row(110.0, 1234.0), "NOVOL": _Row(50.0, None), "ZEROV": _Row(40.0, 0.0)}
    record_live_closes(snap, store, date.today())
    assert store.get_series("TLCPD") == {date.today(): 110.0}
    assert store.get_series("NOVOL") == {date.today(): 50.0}
    assert store.get_series("ZEROV") == {date.today(): 40.0}


def test_prime_from_data912_writes_covered_skips_empty(store):
    class _Prov:
        def fetch_bond_history(self, t):
            if t == "GD46D":
                return [{"date": "2025-12-30", "c": 71.6},
                        {"date": "2026-06-02", "c": 73.0},
                        {"date": "bad", "c": 1.0}]        # fila inválida → se saltea
            return []                                      # no cubierto (bopreal/lecap)
    got = prime_from_data912(["GD46D", "BPY6D"], _Prov(), store)
    assert got == 2
    assert store.get_series("GD46D") == {date(2025, 12, 30): 71.6, date(2026, 6, 2): 73.0}
    assert store.get_series("BPY6D") == {}


def test_prime_from_byma_historico_writes_via_injected_fetch(store):
    """Backfill BYMA: usa la fetch inyectada (sin red) y UPSERT al store. Devuelve
    cierres escritos; un ticker sin serie no aporta."""
    series = {
        "BPB7C": {date(2026, 6, 4): 98.0, date(2026, 6, 5): 98.2},
        "BPC7C": {date(2026, 6, 5): 98.5},
        "NADA":  {},                                   # sin serie → 0
    }
    seen = {}

    def fake_fetch(tk, max_days=400):
        seen[tk] = max_days
        return series.get(tk, {})

    got = prime_from_byma_historico(["bpb7c", "BPC7C", "NADA", ""], store,
                                    max_days=365, fetch=fake_fetch)
    assert got == 3
    assert store.get_series("BPB7C") == {date(2026, 6, 4): 98.0, date(2026, 6, 5): 98.2}
    assert store.get_series("BPC7C") == {date(2026, 6, 5): 98.5}
    assert store.get_series("NADA") == {}
    assert seen.get("BPB7C") == 365 and "" not in seen   # normaliza/upper, dropea vacío


def test_prime_from_byma_historico_normalizes_cer_alias(store):
    """La pata CER de un dual (TXMJ8_CER) se prima/guarda bajo el símbolo de mercado
    (TXMJ8), igual que el read-path — sino quedaría en una clave muerta."""
    captured = {}

    def fake_fetch(tk, max_days=400):
        captured.setdefault("tickers", []).append(tk)
        return {date(2026, 6, 5): 110.0}

    prime_from_byma_historico(["TXMJ8_CER"], store, fetch=fake_fetch)
    assert captured["tickers"] == ["TXMJ8"]                 # fetch del símbolo bare
    assert store.get_series("TXMJ8") == {date(2026, 6, 5): 110.0}
    assert store.get_series("TXMJ8_CER") == {}              # NO bajo la clave alias


def test_byma_prime_candidates_selects_short_unattempted(store):
    """Candidatos = tickers con < min_days cierres, aún no intentados; normaliza
    (_CER → bare), dedup, y excluye los ya cubiertos por Data912."""
    store.upsert("AL30D", {date(2026, 1, d): 60.0 + d for d in range(1, 26)})  # 25 ruedas
    store.upsert("BPB7C", {date(2026, 6, 5): 98.0})                            # 1 rueda
    wanted = ["AL30D", "BPB7C", "BPC7C", "TXMJ8_CER", "bpb7c"]   # dup case + alias
    cands = byma_prime_candidates(wanted, store, attempted=set(), min_days=20)
    assert cands == ["BPB7C", "BPC7C", "TXMJ8"]   # AL30D cubierto; _CER normalizado; dedup
    # los ya intentados no reaparecen (intento 1× por proceso)
    cands2 = byma_prime_candidates(wanted, store, attempted={"BPB7C", "TXMJ8"}, min_days=20)
    assert cands2 == ["BPC7C"]


def test_prime_from_byma_historico_isolates_fetch_failures(store):
    """Un fallo de red por ticker no aborta el resto (best-effort)."""
    def flaky_fetch(tk, max_days=400):
        if tk == "BOOM":
            raise RuntimeError("network down")
        return {date(2026, 6, 5): 100.0}

    got = prime_from_byma_historico(["BOOM", "OKAY"], store, fetch=flaky_fetch)
    assert got == 1
    assert store.get_series("OKAY") == {date(2026, 6, 5): 100.0}
    assert store.get_series("BOOM") == {}


def test_load_error_does_not_latch_empty_cache(store, monkeypatch):
    """Un error transitorio de SQLite en la carga NO debe dejar el cache vacío para
    siempre: la próxima lectura reintenta (sino las variaciones quedan en blanco aun
    con el histórico ya en disco)."""
    import sqlite3
    store.upsert("AL30D", {date(2026, 1, 2): 63.5})   # dato en disco
    store._cache = None                                # forzar recarga
    real_connect = store._connect
    calls = {"n": 0}

    def flaky_connect():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_connect()
    monkeypatch.setattr(store, "_connect", flaky_connect)

    assert store.get_series("AL30D") == {}             # 1ª: falla → vacío, no latchea
    assert store._cache is None
    assert store.get_series("AL30D") == {date(2026, 1, 2): 63.5}  # 2ª: reintenta y carga


def test_fetch_historical_prices_strips_cer_alias(monkeypatch):
    """La pata CER de un dual (TXMJ8_CER) cotiza/se acumula bajo el símbolo de
    mercado (TXMJ8); el lookup de histórico debe stripear el sufijo igual que
    fetch_snapshots, sino devuelve vacío."""
    from core.infrastructure import data912_provider as repo_mod

    prov = repo_mod.Data912MarketDataProvider()
    monkeypatch.setattr(prov, "_load_history", lambda: {})

    class _Store:
        def get_series(self, t):
            return {date(2026, 1, 2): 110.0} if t == "TXMJ8" else {}
    monkeypatch.setattr(repo_mod, "get_price_history_store", lambda: _Store())

    assert prov.fetch_historical_prices("TXMJ8_CER", 0) == {date(2026, 1, 2): 110.0}


def test_provider_merges_csv_and_store(monkeypatch):
    """fetch_historical_prices mergea el CSV legacy (piso) con el store (gana en
    fechas solapadas) — verificado con un store inyectado vía el singleton."""
    from core.infrastructure import data912_provider as repo_mod

    prov = repo_mod.Data912MarketDataProvider()
    monkeypatch.setattr(prov, "_load_history",
                        lambda: {"AL30D": {date(2026, 1, 2): 60.0, date(2026, 1, 3): 61.0}})

    class _Store:
        def get_series(self, t):
            return {date(2026, 1, 3): 99.0, date(2026, 1, 4): 64.0} if t == "AL30D" else {}
    monkeypatch.setattr(repo_mod, "get_price_history_store", lambda: _Store())

    merged = prov.fetch_historical_prices("AL30D", 0)
    assert merged == {date(2026, 1, 2): 60.0,   # solo CSV
                      date(2026, 1, 3): 99.0,   # store gana al CSV
                      date(2026, 1, 4): 64.0}   # solo store
