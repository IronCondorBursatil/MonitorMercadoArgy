"""Familias del catálogo de productos BYMA (core.infrastructure.byma.catalog_products):
cauciones, SENEBI-ON e índices MERVAL/BURCAP. Sin red (cliente fake / fetch inyectable)."""


from core.infrastructure.byma.catalog_products import (
    fetch_cauciones, fetch_index_prices, fetch_senebi_on, index_snapshot,
)


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakePost:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.last = None

    def post(self, url, json=None, headers=None):
        self.last = (url, json)
        return _Resp(self.status, self.payload)


_CAUCIONES = [
    {"symbol": "PESOS-0907-U-CT-ARS", "underlyingSymbol": "PESOS", "denominationCcy": "ARS",
     "daysToMaturity": 1, "maturityDate": "2026-06-09", "settlementPrice": 0.35,
     "closingPrice": 0.34, "volumeAmount": 1e9},
    {"symbol": "PESOS-1507-U-CT-ARS", "denominationCcy": "ARS", "daysToMaturity": 7,
     "closingPrice": 0.38},
    {"trade": 1.0},  # sin symbol → descartada
]


def test_fetch_cauciones_parses_and_sorts_by_term():
    out = fetch_cauciones(client=_FakePost(_CAUCIONES))
    assert [c["days"] for c in out] == [1, 7]            # ordenado por plazo
    assert out[0]["rate"] == 0.35                        # settlementPrice preferido
    assert out[1]["rate"] == 0.38                        # cae a closingPrice
    assert out[0]["ccy"] == "ARS"


def test_fetch_cauciones_http_error_best_effort():
    assert fetch_cauciones(client=_FakePost([], status=500)) == []


_SENEBI = {"content": {}, "data": [
    {"symbol": "A11LD.SB", "denominationCcy": "EXT", "maturityDate": "2028-03-31",
     "daysToMaturity": 666, "closingPrice": 99.0},
    {"symbol": "BBB.SB"},
    {"trade": 0},  # sin symbol
]}


def test_fetch_senebi_parses_and_requests_full_page():
    fake = _FakePost(_SENEBI)
    out = fetch_senebi_on(client=fake)
    assert {o["symbol"] for o in out} == {"A11LD.SB", "BBB.SB"}
    assert fake.last[1].get("page_size") == 5000        # trae todo en una llamada


_INDEX_PRICE = {"content": {}, "data": [
    {"symbol": "M", "description": "S&P MERVAL", "price": 3112024.27,
     "previousClosingPrice": 3084616.64, "variation": 0.0089},
    {"symbol": "G", "description": "S&P BYMA Indice General", "price": 132958165.55,
     "previousClosingPrice": 131587653.63, "variation": 0.0104},
    {"symbol": "SPBYMAIG60", "description": "S&P BYMA Bienes Raices", "price": 113186.31,
     "previousClosingPrice": 109109.68, "variation": 0.0374},
    {"trade": 0},  # sin symbol → descartado
]}


def test_fetch_index_prices_parses_variation():
    out = fetch_index_prices(client=_FakePost(_INDEX_PRICE))
    by = {o["code"]: o for o in out}
    assert by["M"]["name"] == "S&P MERVAL"
    assert by["M"]["last"] == 3112024.27
    assert abs(by["M"]["change_pct"] - 0.89) < 1e-9       # variation 0.0089 → 0.89%
    assert "SPBYMAIG60" in by                              # índice sectorial incluido


def _prices(m_last):
    return [
        {"code": "M", "name": "S&P MERVAL", "last": m_last, "prev": 3084616.0, "change_pct": 0.9},
        {"code": "SPBYMAIG60", "name": "S&P BYMA Bienes Raices", "last": 113186.0,
         "prev": 109109.0, "change_pct": 3.7},
    ]


def test_index_snapshot_accumulates_over_window(tmp_path):
    """TODOS los índices acumulan ticks (franja densa), no solo M/G."""
    import datetime as dt

    from core.infrastructure.byma.index_history import IndexHistoryStore
    store = IndexHistoryStore(str(tmp_path / "idx.db"))

    t0 = dt.datetime(2026, 6, 8, 11, 0, 0)
    snap1 = index_snapshot(prices_fetch=lambda: _prices(3112024.0), store=store,
                           now=t0, prime=False)
    assert [s["code"] for s in snap1][:1] == ["M"]            # headline primero
    m = next(s for s in snap1 if s["code"] == "M")
    assert m["points"] == [3084616.0, 3112024.0]             # 1 muestra → fallback [prev,last]

    # 2º tick (5' después), MERVAL distinto → 2 muestras reales en el store
    t1 = dt.datetime(2026, 6, 8, 11, 5, 0)
    snap2 = index_snapshot(prices_fetch=lambda: _prices(3115000.0), store=store,
                           now=t1, prime=False)
    m2 = next(s for s in snap2 if s["code"] == "M")
    assert m2["points"] == [3112024.0, 3115000.0]           # las 2 muestras acumuladas
    # el sectorial también acumula (mismo valor en t0/t1 → 1 muestra, fallback)
    sec = next(s for s in snap2 if s["code"] == "SPBYMAIG60")
    assert sec["points"] == [109109.0, 113186.0]


def test_index_snapshot_prime_anchors_mg_and_prevclose(tmp_path, monkeypatch):
    """Prime: ancla M con cierres diarios del chart + cierre previo de los 16."""
    import datetime as dt

    import core.infrastructure.byma.catalog_products as cpmod
    import core.infrastructure.byma.chart_history as chmod
    from core.infrastructure.byma.index_history import IndexHistoryStore

    cpmod._MG_PRIMED = False
    monkeypatch.setattr(chmod, "fetch_index_history", lambda code, max_days=90: (
        {dt.date(2026, 6, 4): 3000000.0, dt.date(2026, 6, 5): 3050000.0} if code == "M" else {}))
    store = IndexHistoryStore(str(tmp_path / "idx.db"))
    snap = index_snapshot(prices_fetch=lambda: _prices(3112024.0), store=store,
                          now=dt.datetime(2026, 6, 8, 11, 0, 0), prime=True)
    m = next(s for s in snap if s["code"] == "M")
    # 2 anclas diarias del chart + cierre previo + tick de hoy
    assert m["points"][:2] == [3000000.0, 3050000.0]
    assert 3084616.0 in m["points"] and m["points"][-1] == 3112024.0
