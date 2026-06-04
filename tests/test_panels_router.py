"""Router de paneles HTMX (Fase 4 web): formato de celdas, build de filas y rutas."""

import re
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from apps.web.app import app
from apps.web.routers import panels
from core.domain.models import Cashflow, Instrument, MarketSnapshot, InstrumentMetrics


def test_hcol_css_covers_every_column():
    """Las clases hcol-N (ocultar columna del Config) deben llegar al menos hasta el
    panel más ancho, sino el toggle de las últimas columnas 'no opera' (ej. 1A en
    soberanos = col 14). Ata la CSS al nº real de columnas para que no se desincronice."""
    max_cols = max(len(cols) for (_t, _types, cols) in panels.PANELS.values())
    css = (Path(panels.__file__).resolve().parents[1] / "static" / "css" / "app.css").read_text(encoding="utf-8")
    max_hcol = max(int(n) for n in re.findall(r"hcol-(\d+)", css))
    assert max_hcol >= max_cols, f"hcol-N llega a {max_hcol} pero hay paneles de {max_cols} columnas"


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


def test_bonares_panel_has_return_columns_colored():
    # Soberanos USD/Bopreales llevan las ventanas de rendimiento (Sem/1M/3M/YTD/1A).
    labels = [c["label"] for c in panels._SOBERANO_USD_COLS]
    assert labels[labels.index("%Día") + 1: labels.index("Vol $")] == \
        ["Sem", "1M", "3M", "YTD", "1A"]

    m = _metric("AL30D", "BONAR", 63.5, 0.12, 3.0, 70.0, 0.9)
    m.variance_7d, m.variance_30d = 0.0012, -0.0234   # +0.12% / -2.34%
    m.variance_90d, m.variance_ytd = 0.05, None
    m.variance_1y = 0.0929                             # +9.29% (GD46 a 1 año)
    rows = panels._build_rows("bonares", _StubState([m]))
    cells = {c["text"]: c["cls"] for c in rows[0]["cells"]}
    assert cells["+0.12%"] == "pos"
    assert cells["-2.34%"] == "neg"
    assert cells["+5.00%"] == "pos"
    assert cells["+9.29%"] == "pos"
    assert "—" in cells                                # YTD sin dato → "—" sin color

    # Dólar Linked NO las lleva (sin histórico): mismas columnas que _BONARES_COLS.
    assert panels.PANELS["dolar_linked"][2] is panels._BONARES_COLS
    assert "Sem" not in [c["label"] for c in panels._BONARES_COLS]


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


def test_implied_rates_matches_a3_report():
    # jun-26 del informe A3: fut 1457.5, spot 1438.5, 27 días → TNA 17.9 / TEA 19.4 / crawl 1.5.
    tna, tea, crawl = panels._implied_rates(1457.5, 1438.5, 27)
    assert round(tna * 100, 1) == 17.9
    assert round(tea * 100, 1) == 19.4
    assert round(crawl * 100, 1) == 1.5
    # inputs faltantes / inválidos → (None, None, None) sin romper.
    assert panels._implied_rates(None, 1438.5, 27) == (None, None, None)
    assert panels._implied_rates(1457.5, 0, 27) == (None, None, None)
    assert panels._implied_rates(1457.5, 1438.5, 0) == (None, None, None)


def test_futuros_label():
    assert panels._futuros_label("DLR/JUN26") == "jun-26"
    assert panels._futuros_label("DLR/ABR27") == "abr-27"


def _lecap(ticker, days, tir):
    today = date.today()
    inst = Instrument(ticker=ticker, short_name=ticker, instrument_type="LECAP",
                      maturity_date=today + timedelta(days=days),
                      cashflows=[Cashflow(today + timedelta(days=days), 100.0, 0.0)])
    snap = MarketSnapshot(instrument=inst, price=100.0, last_update=today,
                          change_pct=0.0, volume=1_000.0)
    return InstrumentMetrics(snapshot=snap, tir=tir, duration=days / 365.0,
                             technical_value=100.0, parity=1.0)


def test_peso_tea_curve_interpolates():
    today = date.today()
    curve = panels._peso_tea_curve(_StubState([_lecap("S1", 30, 0.40), _lecap("S2", 120, 0.30)]), today)
    assert curve is not None
    assert abs(curve(30) - 0.40) < 1e-9        # extremo bajo
    assert abs(curve(75) - 0.35) < 1e-9        # interpolación lineal en el medio
    assert abs(curve(400) - 0.30) < 1e-9       # extrapolación plana al extremo alto
    # <2 puntos → None (la columna Carry queda en "—")
    assert panels._peso_tea_curve(_StubState([_lecap("S1", 30, 0.40)]), today) is None
    # tipos que no son tasa fija no entran a la curva peso
    assert panels._peso_tea_curve(
        _StubState([_metric("AL30", "BONAR", 70, 0.1, 3, 80, 0.9)]), today) is None


class _FixedDate(date):
    @classmethod
    def today(cls):
        return date(2026, 6, 4)


class _RofexStub:
    def get_quotes(self, syms):
        return {"DLR/JUN26": {"last": 1457.5, "settle": 1458.0, "prev_settle": 1454.0,
                              "open_interest": 1383665, "volume": 1.04e11},
                "DLR/DIC26": {"last": 1632.5, "settle": 1630.0, "prev_settle": 1628.0,
                              "open_interest": 165007, "volume": 3.3e8}}


class _FxStub:
    def get_mayorista_mid(self):
        return 1438.5


class _IdxStub:
    def get_a3500(self):
        return 1438.5


def test_build_futuros_share_resilient_and_shape(monkeypatch):
    # Sin rofex / rofex que rompe → {} (degrada solo, nunca tira el popup).
    assert panels._build_futuros_share(None, None, None, None) == {}

    class _Boom:
        def get_quotes(self, syms):
            raise RuntimeError("ws down")

    assert panels._build_futuros_share(_Boom(), None, None, None) == {}

    monkeypatch.setattr(panels, "date", _FixedDate)   # fija "hoy" → días al vto estables
    data = panels._build_futuros_share(_RofexStub(), _FxStub(), _IdxStub(), _StubState([]))
    assert data["spot"] == 1438.5
    assert data["has_peso_curve"] is False
    jun = next(c for c in data["contracts"] if c["label"] == "jun-26")
    # valor exacto de TEA depende de los días al vto (cubierto por test_implied_rates);
    # acá chequeamos forma: TEA en rango razonable y TNA lineal < TEA compuesta.
    assert jun["tea"] is not None and 18.0 < jun["tea"] < 22.0
    assert jun["tna"] < jun["tea"]
    assert jun["var1d"] is not None and jun["carry"] is None   # sin curva peso → carry None
    assert round(jun["basis"], 1) == 19.0                      # 1457.5 − 1438.5


def test_futuros_share_route(monkeypatch):
    from apps.web.deps import get_rofex, get_fx, get_indices, get_state

    monkeypatch.setattr(panels, "date", _FixedDate)
    app.dependency_overrides[get_rofex] = lambda: _RofexStub()
    app.dependency_overrides[get_fx] = lambda: _FxStub()
    app.dependency_overrides[get_indices] = lambda: _IdxStub()
    app.dependency_overrides[get_state] = lambda: _StubState([])
    try:
        with TestClient(app) as c:
            r = c.get("/panels/futuros/share")
            assert r.status_code == 200
            for s in ("Curva Rofex", "Carry", "Cobertura", "futCv-v1", "jun-26"):
                assert s in r.text
    finally:
        app.dependency_overrides.clear()


def test_bei_rows_scaling_and_empty():
    class _S:
        def bei_tables(self):
            return {"tenor": [{
                "plazo": "3M", "dias": 91, "tea_nominal": 0.25, "tea_real": -0.05,
                "tamar_fwd": 0.23, "bei_spot": 0.31, "bei_fwd": 0.30, "bei_g_adj": 0.29,
                "bei_tamar": 0.30, "dev_implicita": 0.40, "tc_real": -0.02,
            }]}

    rows = panels._build_bei_rows("bei_tenor", _S())
    assert len(rows) == 1
    assert any(c["text"] == "25.00%" for c in rows[0]["cells"])   # 0.25 decimal → 25.00%
    assert rows[0]["clickable"] is False

    class _E:
        def bei_tables(self):
            return None

    assert panels._build_bei_rows("bei_tenor", _E()) == []


def test_index_and_fragment_routes():
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        for title in ("BONARES Y GLOBALES", "BONOS CER", "TASA FIJA", "TAMAR / DUAL"):
            assert title in r.text
        assert 'hx-get="/panels/bonares/rows"' in r.text
        assert c.get("/panels/cer/rows").status_code == 200
        assert c.get("/panels/tamar/rows").status_code == 200
