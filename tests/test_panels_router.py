"""Router de paneles HTMX (Fase 4 web): formato de celdas, build de filas y rutas."""

from datetime import date, timedelta

from fastapi.testclient import TestClient

from apps.web.app import app
from apps.web.routers import panels
from core.domain.models import Cashflow, Instrument, MarketSnapshot, InstrumentMetrics


def test_fmt_kinds():
    assert panels._fmt(None, "number") == "—"
    assert panels._fmt(1234.5, "number", 2) == "1,234.50"
    assert panels._fmt(23.456, "percent", 2) == "23.46%"
    assert panels._fmt(1.2, "percent_signed", 2) == "+1.20%"
    assert panels._fmt(-1.2, "percent_signed", 2) == "-1.20%"
    assert panels._fmt(1_500_000, "volume") == "1.5M"
    assert panels._fmt(date(2026, 5, 29), "date") == "29/05/26"
    assert panels._fmt("AL30", "text") == "AL30"


class _StubState:
    def __init__(self, metrics):
        self._m = metrics

    def metrics(self):
        return self._m


def _metric(ticker, itype, price, tir, md, vtec, parity):
    inst = Instrument(ticker=ticker, short_name=ticker, instrument_type=itype,
                      maturity_date=date.today() + timedelta(days=400),
                      cashflows=[Cashflow(date.today() + timedelta(days=400), 100.0, 0.0)])
    snap = MarketSnapshot(instrument=inst, price=price, last_update=date.today(),
                          change_pct=1.5, volume=2_000_000.0)
    return InstrumentMetrics(snapshot=snap, tir=tir, duration=md,
                             technical_value=vtec, parity=parity)


def test_build_rows_for_cer_panel():
    state = _StubState([
        _metric("TX26", "BONCER", 95.0, 0.10, 1.2, 100.0, 0.95),
        _metric("AL30", "BONAR", 70.0, 0.15, 3.0, 80.0, 0.875),  # no es CER → excluido
    ])
    rows = panels._build_rows("cer", state)
    assert len(rows) == 1
    assert rows[0]["ticker"] == "TX26"
    # primera celda = ticker; columnas CER incluyen category, vto, etc.
    assert rows[0]["cells"][0]["text"] == "TX26"
    # TIR 0.10 -> 10.00%
    tir_cell = next(c for c in rows[0]["cells"] if c["text"] == "10.00%")
    assert tir_cell is not None


def test_valor_relativo_rich_cheap():
    # Curva CER (peso real, flavor único): 3 en curva + 1 claramente barato.
    state = _StubState([
        _metric("A1", "BONCER", 100, 0.10, 1.0, 100, 1.0),
        _metric("A2", "BONCER", 100, 0.12, 2.0, 100, 1.0),
        _metric("A3", "BONCER", 100, 0.13, 3.0, 100, 1.0),
        _metric("A4", "BONCER", 100, 0.30, 2.0, 100, 1.0),  # cheap: spread > 0
    ])
    rv = panels._rv_map(state)
    assert rv["A4"]["spread"] is not None and rv["A4"]["spread"] > 0
    rows = panels._build_rv_rows(state)
    assert rows and rows[0]["ticker"] == "A4"  # el más barato va primero
    # Los soberanos hard-dollar NO entran al rich/cheap (curvas peso únicamente).
    sob = _StubState([_metric("AL30", "BONAR", 100, 0.10, 1.0, 100, 1.0)])
    assert panels._build_rv_rows(sob) == []


def test_valor_relativo_too_few_points_no_spread():
    state = _StubState([_metric("X1", "BONCER", 100, 0.10, 1.0, 100, 1.0)])
    assert panels._build_rv_rows(state) == []  # <3 puntos → sin fit


def test_panel_lider_rows_from_stub_provider():
    from core.domain.instrument_groups import PANEL_LIDER
    from core.domain.models import MarketSnapshot

    class _StubProv:
        def fetch_snapshots(self, tickers):
            return {tickers[0]: MarketSnapshot(price=100.0, bid=99.0, ask=101.0,
                                               volume=1_000_000.0, operations=50, change_pct=1.2)}

    rows = panels._build_panel_lider_rows(_StubProv())
    assert len(rows) == 1
    assert rows[0]["ticker"] == PANEL_LIDER[0]
    assert rows[0]["clickable"] is False                     # acciones no abren popup
    assert any(c["text"] == "100.00" for c in rows[0]["cells"])  # mid = (99+101)/2
    assert panels._build_panel_lider_rows(None) == []        # sin provider → vacío


def test_futuros_rows_resilient():
    # Sin rofex → vacío; rofex que falla → vacío (nunca rompe el panel).
    assert panels._build_futuros_rows(None, None, None) == []

    class _Boom:
        def get_quotes(self, syms):
            raise RuntimeError("ws down")

    assert panels._build_futuros_rows(_Boom(), None, None) == []


def test_index_and_fragment_routes():
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        for title in ("BONARES Y GLOBALES", "BONOS CER", "TASA FIJA", "TAMAR / DUAL"):
            assert title in r.text
        assert 'hx-get="/panels/bonares/rows"' in r.text
        assert c.get("/panels/cer/rows").status_code == 200
        assert c.get("/panels/tamar/rows").status_code == 200
