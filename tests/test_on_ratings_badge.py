"""Badge de cambio de calificación en el panel ON.

Cubre las tres piezas de la feature (spec `2026-08-31-fix-ratings-monitor-design.md`):

1. `on_service`: cada fila del dataset expone `rating_chg={dir,from,to,fecha}` cuando el
   emisor cambió de calificación en la última semana, y `None` cuando no. El join se hace
   por el emisor CANÓNICO que devuelve `rating_for()` (el nombre del listado FIX), no por
   el `short_name` del catálogo: el catálogo dice "YPF SA" y FIX "YPF S.A.", así que un
   join por el nombre crudo no encontraría nunca el cambio.
2. `meta.ratings_as_of` sale de `ratings.as_of()` (función, corte real del store), no de
   la constante `AS_OF` del CSV.
3. El bundle `static/js/on.js` está regenerado (`scripts/build_on_static.py`) con el badge
   que vive en `on_src/unified.js` — el bundle es artefacto, la fuente es on_src.

El store se ejerce DE VERDAD (SQLite en tmp_path, dos cortes reales) en vez de mockear
`recent_changes`: así el test también ata la clasificación up/down/watch tal como la
produce el diff, que es lo que el badge pinta.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

import apps.web.on_service as on_service
from core.domain.models import Cashflow, Instrument, InstrumentMetrics, MarketSnapshot
from core.infrastructure import ratings as ratings_mod
from core.infrastructure import ratings_history as rh_mod

ROOT = Path(__file__).resolve().parent.parent
UNIFIED_SRC = ROOT / "apps" / "web" / "on_src" / "unified.js"
ON_BUNDLE = ROOT / "apps" / "web" / "static" / "js" / "on.js"

# Emisores tal cual figuran en data/calificaciones.csv (= nombre canónico del matcher).
YPF = "YPF S.A."
PAMPA = "Pampa Energía S.A."


class _StubState:
    """AppState mínimo: sólo `metrics()` + `revision` (lo que consume on_service)."""

    def __init__(self, metrics, revision=1):
        self._m, self._rev = metrics, revision

    def metrics(self):
        return self._m

    @property
    def revision(self):
        return self._rev


def _on(ticker, emisor, *, price=100.0):
    inst = Instrument(ticker=ticker, short_name=emisor, instrument_type="HARD DOLLAR",
                      maturity_date=date.today() + timedelta(days=400),
                      cashflows=[Cashflow(date.today() + timedelta(days=400), 100.0, 0.0)])
    snap = MarketSnapshot(instrument=inst, price=price, last_update=date.today(),
                          change_pct=1.0, volume=1_000.0)
    return InstrumentMetrics(snapshot=snap, tir=0.07, duration=2.0,
                             technical_value=100.0, parity=0.95)


def _fila(entidad, rating, persp="Estable"):
    return {entidad: {"rating": rating, "perspectiva": persp,
                      "area": "Finanzas Corporativas", "sector": "Petróleo y Gas"}}


def _clear_ratings_cache():
    """Invalida los caches de `ratings` (lru_cache por proceso). Necesario porque el
    matcher puede fusionar el store en `_entries()`: un test que cambia el store no debe
    heredar el resultado del anterior. Tolerante a que `_entries` deje de ser lru_cache."""
    for name in ("rating_for", "_entries"):
        fn = getattr(ratings_mod, name, None)
        clear = getattr(fn, "cache_clear", None)
        if clear:
            clear()


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Store real sobre SQLite temporal, enchufado donde on_service lo busca."""
    st = rh_mod.RatingsHistoryStore(tmp_path / "ratings_history.db")
    monkeypatch.setattr(rh_mod, "get_ratings_history_store", lambda: st)
    on_service.clear_cache()
    _clear_ratings_cache()
    yield st
    on_service.clear_cache()
    _clear_ratings_cache()


def _dataset(metrics):
    return on_service.get_on_dataset(_StubState(metrics), None, force=True)


def _by_ticker(data):
    return {b["ticker"]: b for b in data["bonds"]}


def _dos_cortes(st, ayer_rows, hoy_rows):
    """Dos cortes reales (ayer/hoy): el segundo materializa el diff con fecha = hoy,
    que es la que mira la ventana de 7 días del badge."""
    hoy = date.today()
    assert st.record_corte(ayer_rows, hoy - timedelta(days=1))["status"] == "ok"
    assert st.record_corte(hoy_rows, hoy)["status"] == "ok"


def test_rating_chg_upgrade_se_joinea_por_emisor_canonico(store):
    """El catálogo trae "YPF SA" y FIX "YPF S.A.": el join usa el emisor canónico del
    matcher, así que el upgrade aparece igual en la fila."""
    _dos_cortes(store, _fila(YPF, "AA(arg)"), _fila(YPF, "AAA(arg)"))
    b = _by_ticker(_dataset([_on("YMCXD", "YPF SA")]))["YMCXD"]
    assert b["rating_chg"] == {
        "dir": "up", "from": "AA(arg)", "to": "AAA(arg)",
        "fecha": date.today().isoformat(),
        "persp_from": "Estable", "persp_to": "Estable",
    }


def test_rating_chg_downgrade(store):
    _dos_cortes(store, _fila(YPF, "AA(arg)"), _fila(YPF, "BBB(arg)"))
    b = _by_ticker(_dataset([_on("YMCXD", "YPF SA")]))["YMCXD"]
    assert b["rating_chg"]["dir"] == "down"
    assert (b["rating_chg"]["from"], b["rating_chg"]["to"]) == ("AA(arg)", "BBB(arg)")


def test_rating_chg_watch_cuando_solo_cambia_la_perspectiva(store):
    """Mismo rating, perspectiva Estable→RW Negativo: cambio SIN dirección (watch)."""
    _dos_cortes(store, _fila(YPF, "AAA(arg)", "Estable"),
                _fila(YPF, "AAA(arg)", "RW Negativo"))
    chg = _by_ticker(_dataset([_on("YMCXD", "YPF SA")]))["YMCXD"]["rating_chg"]
    assert chg["dir"] == "watch"
    assert (chg["persp_from"], chg["persp_to"]) == ("Estable", "RW Negativo")


def test_emisor_sin_cambio_reciente_expone_none(store):
    """Dos emisores en el corte, uno solo cambió: el otro no se contagia el badge."""
    ayer = {**_fila(YPF, "AA(arg)"), **_fila(PAMPA, "AAA(arg)")}
    hoy = {**_fila(YPF, "AAA(arg)"), **_fila(PAMPA, "AAA(arg)")}
    _dos_cortes(store, ayer, hoy)
    rows = _by_ticker(_dataset([_on("YMCXD", "YPF SA"), _on("MGC9O", "Pampa Energía SA")]))
    assert rows["YMCXD"]["rating_chg"]["dir"] == "up"
    assert rows["MGC9O"]["rating_chg"] is None


def test_sin_cortes_en_el_store_todas_las_filas_sin_badge(store):
    """Store vacío (todavía no corrió el loop): el panel se arma igual, sin badges."""
    b = _by_ticker(_dataset([_on("YMCXD", "YPF SA")]))["YMCXD"]
    assert b["rating_chg"] is None
    assert b["rating"]                      # el rating (CSV/store) sigue estando


def test_cambio_de_un_emisor_que_no_esta_en_el_panel_no_rompe(store):
    """Un cambio de una entidad que no cotiza ON no ensucia ninguna fila ni explota."""
    _dos_cortes(store, _fila("Banco Fantasma S.A.", "A(arg)"),
                _fila("Banco Fantasma S.A.", "A+(arg)"))
    data = _dataset([_on("YMCXD", "YPF SA"), _on("MGC9O", "Pampa Energía SA")])
    assert len(data["bonds"]) == 2
    assert all(b["rating_chg"] is None for b in data["bonds"])


def test_emisor_sin_calificacion_no_tiene_badge(store):
    """Sin match en el listado no hay emisor canónico contra el que joinear el cambio."""
    _dos_cortes(store, _fila(YPF, "AA(arg)"), _fila(YPF, "AAA(arg)"))
    b = _by_ticker(_dataset([_on("ZZZD", "Frobnicate SA")]))["ZZZD"]
    assert b["rating"] is None and b["rating_chg"] is None


def test_ratings_as_of_sale_de_la_funcion_no_de_la_constante(store, monkeypatch):
    """`meta.ratings_as_of` delega en `ratings.as_of()` (corte real del store); la
    constante `AS_OF` del CSV pasó a ser sólo el fallback interno de esa función."""
    monkeypatch.setattr(ratings_mod, "as_of", lambda: "2099-12-31", raising=False)
    data = _dataset([_on("YMCXD", "YPF SA")])
    assert data["meta"]["ratings_as_of"] == "2099-12-31"


# --------------------------------------------------------------------------- #
# Front: el badge vive en on_src/unified.js y el bundle está regenerado.
# --------------------------------------------------------------------------- #
def test_unified_src_pinta_el_badge_escapado():
    src = UNIFIED_SRC.read_text(encoding="utf-8")
    assert "rating_chg" in src                       # consume el campo del dataset
    assert "uni-chg-" in src                         # clase por dirección (up/down/watch)
    assert "▲" in src and "▼" in src                 # flechas up/down
    assert "ON.esc(" in src.split("rating_chg")[1]   # el title va escapado


def test_bundle_on_js_regenerado_con_el_badge():
    """Espejo on_src → static/js/on.js: si se editó la fuente sin correr
    scripts/build_on_static.py, el badge no llega al navegador."""
    src = UNIFIED_SRC.read_text(encoding="utf-8")
    bundle = ON_BUNDLE.read_text(encoding="utf-8")
    frag = [ln.strip() for ln in src.splitlines() if "uni-chg-" in ln or "rating_chg" in ln]
    assert frag, "unified.js debería pintar el badge"
    for ln in frag:
        assert ln in bundle, f"bundle desactualizado, falta: {ln[:60]}…"


def test_css_del_badge_tiene_las_tres_direcciones():
    css = (ROOT / "apps" / "web" / "static" / "css" / "on.css").read_text(encoding="utf-8")
    for cls in (".uni-chg-up", ".uni-chg-down", ".uni-chg-watch"):
        assert cls in css, cls
