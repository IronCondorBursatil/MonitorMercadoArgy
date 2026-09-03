"""Auditoría D2 — la columna "Sector" del panel PROVINCIALES era una columna muerta.

`provinciales` reusa `_ON_COLS` (que trae "Sector"), pero `_build_rows` sólo puebla
la clave `sector` para `obligaciones_negociables`: las N celdas del panel subsoberano
salían siempre en "—", con su `<th>` y su toggle en el popup Config operando sobre
nada. Poblarla tampoco sirve: `sector_for()` es la taxonomía de emisores CORPORATIVOS
(`on_classification`), donde los subsoberanos caen todos en "Otros".
"""

from __future__ import annotations

from datetime import date, timedelta

import apps.web.panels_rows as panels_rows
from apps.web.routers import panels_schema
from core.domain.models import Cashflow, Instrument, InstrumentMetrics, MarketSnapshot


class _StubState:
    def __init__(self, metrics):
        self._m = metrics

    def metrics(self):
        return self._m


def _provincial(ticker="BA37D"):
    vto = date.today() + timedelta(days=900)
    inst = Instrument(ticker=ticker, short_name="PROVINCIA DE BUENOS AIRES",
                      instrument_type="PROVINCIAL HARD DOLLAR", maturity_date=vto,
                      cashflows=[Cashflow(vto, 100.0, 5.0)])
    snap = MarketSnapshot(instrument=inst, price=62.0, last_update=date.today(),
                          change_pct=0.4, volume=1_000_000.0)
    return InstrumentMetrics(snapshot=snap, tir=0.11, duration=3.4,
                             technical_value=100.0, parity=0.62)


def test_el_panel_provinciales_no_declara_la_columna_sector():
    cols = panels_rows.panel_columns("provinciales")
    assert "sector" not in [c["key"] for c in cols], \
        "columna muerta: sector_for() sólo clasifica emisores corporativos"
    # el panel de ONs, que sí la puebla, la conserva
    assert "sector" in [c["key"] for c in panels_rows.panel_columns(
        "obligaciones_negociables")]
    # y no se toca el schema compartido (_ON_COLS lo usan los dos paneles)
    assert "sector" in [c["key"] for c in panels_schema._ON_COLS]


def test_las_filas_de_provinciales_no_traen_celdas_muertas():
    rows = panels_rows._build_rows("provinciales", _StubState([_provincial()]))
    assert len(rows) == 1
    cols = panels_rows.panel_columns("provinciales")
    cells = rows[0]["cells"]
    assert len(cells) == len(cols)
    assert [c["key"] for c in cells] == [c["key"] for c in cols]
    assert "sector" not in [c["key"] for c in cells]


# ── Alineación header ↔ celdas, en LOS 14 PANELES ──────────────────────────
# El `<th>` del index y el `ncols` del fragmento salen de `panel_columns(pid)`; las
# celdas las arman builders distintos según el panel (los especiales —VR, panel
# líder, futuros, BEI— ni siquiera pasan por `panel_columns`: leen `_VR_COLS`,
# `_PANEL_LIDER_COLS`, `_FUTUROS_COLS` o `PANELS[pid][2]` directo). Si alguna de
# esas listas se desincroniza del header, el toggle `hcol-N` del Config oculta la
# columna equivocada y las celdas se corren una posición.


def _bono(ticker, tipo, dur=2.0, tir=0.30):
    vto = date.today() + timedelta(days=700)
    inst = Instrument(ticker=ticker, short_name="EMISOR " + ticker,
                      instrument_type=tipo, maturity_date=vto,
                      cashflows=[Cashflow(vto, 100.0, 3.0)])
    snap = MarketSnapshot(instrument=inst, price=95.0, last_update=date.today(),
                          change_pct=0.5, volume=1_000_000.0)
    return InstrumentMetrics(snapshot=snap, tir=tir, duration=dur,
                             technical_value=100.0, parity=0.95)


class _StubProvider:
    """Panel líder: `fetch_snapshots` del cache de Data912."""

    def fetch_snapshots(self, tickers):
        from types import SimpleNamespace
        return {tk: SimpleNamespace(bid=10.0, ask=11.0, price=10.5, change_pct=1.0,
                                    volume=5000.0, operations=12)
                for tk in tickers}


class _StubRofex:
    def get_quotes(self, symbols):
        return {sym: {"bid": 1400.0, "ask": 1410.0, "last": 1405.0, "settle": 1404.0,
                      "open_interest": 100, "volume": 50} for sym in symbols}


class _StubFx:
    def get_mayorista_mid(self):
        return 1400.0


class _StubIndices:
    def get_a3500(self):
        return 1400.0


class _StubStateFull(_StubState):
    """State con métricas + tablas BEI (los paneles BEI leen `bei_tables()`)."""

    def __init__(self, metrics, bei=None):
        super().__init__(metrics)
        self._bei = bei

    def bei_tables(self):
        return self._bei


_BEI_TABLES = {
    "tenor": [{"plazo": "6m", "dias": 180, "tea_nominal": 0.5, "tea_real": 0.1}],
    "sendero": [{"mes": "2026-10", "dias_mes": 31, "bei_mensual": 0.02}],
    "pares": [{"lecap": "S30S5", "boncer": "TZXD5", "dias": 90}],
}


def _rows_para(pid, state):
    """Filas por el MISMO camino que `panels.panel_rows` (que special-casea futuros)."""
    if pid == "futuros":
        return panels_rows._build_futuros_rows(_StubRofex(), _StubFx(), _StubIndices())
    if pid in ("valor_relativo", "panel_lider") or pid.startswith("bei_"):
        return panels_rows._build_rows(pid, state, _StubProvider())
    # Paneles de bonos: se les inyecta una fila propia (el stub no tiene el tipo de
    # cada panel), que es justo lo que hace el camino CI del router.
    return panels_rows._build_rows(pid, state, _StubProvider(),
                                   metrics_override=[_bono("XX01", "BONAR")])


def test_el_header_del_dashboard_coincide_con_las_celdas():
    """`len(panel_columns(pid))` == celdas de CADA fila, panel por panel."""
    from apps.web.routers.panels_schema import PANEL_ORDER

    # 3 LECAPs (mismo grupo/moneda) para que el fit log de valor_relativo dé filas.
    metrics = [_bono("S30S5", "LECAP", dur=0.3, tir=0.35),
               _bono("S15D5", "LECAP", dur=0.6, tir=0.33),
               _bono("T13F6", "LECAP", dur=1.1, tir=0.31)]
    state = _StubStateFull(metrics, _BEI_TABLES)

    vistos = []
    for pid in PANEL_ORDER:
        cols = panels_rows.panel_columns(pid)
        rows = _rows_para(pid, state)
        assert rows, f"{pid}: el stub no produjo filas (el test no probaría nada)"
        vistos.append(pid)
        for r in rows:
            assert len(r["cells"]) == len(cols), (
                f"{pid}: header con {len(cols)} columnas y fila con "
                f"{len(r['cells'])} celdas → el toggle hcol-N oculta otra columna")
    assert vistos == PANEL_ORDER, "quedaron paneles sin verificar"


def test_las_celdas_de_provinciales_estan_en_el_orden_del_header():
    """Además del largo: mismas claves y en el mismo orden (el `<th>` es posicional)."""
    cols = panels_rows.panel_columns("provinciales")
    rows = panels_rows._build_rows("provinciales", _StubState([_provincial()]))
    assert [c["key"] for c in rows[0]["cells"]] == [c["key"] for c in cols]
