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


def test_index_and_fragment_routes():
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        for title in ("BONARES Y GLOBALES", "BONOS CER", "TASA FIJA", "TAMAR / DUAL"):
            assert title in r.text
        assert 'hx-get="/panels/bonares/rows"' in r.text
        assert c.get("/panels/cer/rows").status_code == 200
        assert c.get("/panels/tamar/rows").status_code == 200
