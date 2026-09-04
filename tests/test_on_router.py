"""Router /on: la página carga la app cliente; /on/data arma el dataset (mismo shape
que window.ON_DATA) desde el snapshot vivo, clasificando por sector al vuelo."""

from datetime import date, timedelta

from fastapi.testclient import TestClient

import apps.web.on_service as on_service
from apps.web.app import app
from apps.web.deps import get_fx, get_state
from core.domain.models import Cashflow, Instrument, InstrumentMetrics, MarketSnapshot


class _StubFx:
    """FX fijo para tests (sin red). rate = pesos por USD."""
    def __init__(self, rate=None):
        self._r = rate

    def get_mep_venta(self):
        return self._r

    def get_ccl_venta(self):
        return self._r

    def get_mayorista_venta(self):
        return self._r


class _StubState:
    def __init__(self, metrics, revision=1, ciclo=None):
        self._m = metrics
        self._rev = revision
        # El memo de `on_service` va por `last_refresh` (el sello del ciclo del refresh
        # loop), no por `revision`: la revision esta gateada por la huella de los campos
        # de MERCADO y este dataset depende ademas del CATALOGO, asi que una edicion del
        # ABM no la movia y el panel servia datos viejos por horas. Cada stub estrena
        # sello para que dos datasets distintos no compartan entrada de cache.
        _StubState._n += 1
        self._ciclo = ciclo if ciclo is not None else ("test", _StubState._n)

    _n = 0

    def metrics(self):
        return self._m

    @property
    def revision(self):
        return self._rev

    @property
    def last_refresh(self):
        return self._ciclo


def _on(ticker, emisor, *, price=100.0, tir=0.07, md=2.0, vtec=100.0, parity=0.95,
        ley=None, itype="HARD DOLLAR", change=1.23, volume=2_000_000.0, days=400, clase=None,
        sector=None, isin=None):
    inst = Instrument(ticker=ticker, short_name=emisor, instrument_type=itype,
                      maturity_date=date.today() + timedelta(days=days),
                      ley_aplicable=ley, serie_clase=clase, sector_override=sector,
                      isin=isin,
                      cashflows=[Cashflow(date.today() + timedelta(days=days), 100.0, 0.0)])
    snap = MarketSnapshot(instrument=inst, price=price, last_update=date.today(),
                          change_pct=change, volume=volume)
    return InstrumentMetrics(snapshot=snap, tir=tir, duration=md,
                             technical_value=vtec, parity=parity)


def _fetch(stub, fx=None):
    on_service.clear_cache()
    app.dependency_overrides[get_state] = lambda: stub
    app.dependency_overrides[get_fx] = lambda: fx   # sin red: fx stub (o None)
    try:
        with TestClient(app) as c:
            r = c.get("/on/data")
        assert r.status_code == 200
        return r.json()
    finally:
        app.dependency_overrides.pop(get_state, None)
        app.dependency_overrides.pop(get_fx, None)
        on_service.clear_cache()


def test_on_page_loads_client_app():
    with TestClient(app) as c:
        r = c.get("/on")
    assert r.status_code == 200
    assert "/static/js/on.js" in r.text and 'id="subtab-bar"' in r.text


def test_on_data_shape_and_meta():
    data = _fetch(_StubState([_on("YMCXD", "YPF SA", ley="Extranjera")]))
    for k in ("generated", "today", "bonds", "sectors", "meta"):
        assert k in data
    for k in ("n_bonds", "n_ar", "n_ext", "n_legs", "ccys", "fx_note"):
        assert k in data["meta"]
    assert data["meta"]["ccys"] == ["ARS", "MEP", "CABLE"]


def test_on_data_classifies_sector():
    data = _fetch(_StubState([
        _on("YMCXD", "YPF SA"),         # Energía
        _on("EDNXD", "EDENOR SA"),      # Utilities
        _on("ZZZD", "Frobnicate SA"),   # Otros
    ]))
    by_tk = {b["ticker"]: b for b in data["bonds"]}
    assert by_tk["YMCXD"]["sector"] == "Energía / Petróleo & Gas"
    assert by_tk["EDNXD"]["sector"] == "Utilities (Luz / Gas)"
    assert by_tk["ZZZD"]["sector"] == "Otros"


def test_on_data_sector_override():
    """La categoría elegida a mano en el ABM (sector_override) gana sobre el match
    por emisor; sin override, se deduce del emisor."""
    data = _fetch(_StubState([
        _on("YMCXD", "YPF SA"),                                       # → Energía (emisor)
        _on("ZZZD", "Frobnicate SA", sector="Servicios Financieros"),  # override manual
    ]))
    by = {b["ticker"]: b for b in data["bonds"]}
    assert by["YMCXD"]["sector"] == "Energía / Petróleo & Gas"
    assert by["ZZZD"]["sector"] == "Servicios Financieros"


def test_on_data_percent_scaling():
    data = _fetch(_StubState([_on("YMCXD", "YPF SA", tir=0.115, parity=0.95, md=2.5,
                                  price=99.0, change=1.23)]))
    b = data["bonds"][0]
    assert b["tir"] == 11.5            # decimal → %
    assert b["paridad"] == 95.0        # decimal → %
    assert b["md"] == 2.5             # sin tocar
    assert b["price"] == 99.0         # sin tocar
    assert b["change_pct"] == 1.23    # ya en %, passthrough


def test_on_data_tipo_hard_dollar_vs_dollar_linked():
    data = _fetch(_StubState([
        _on("YMCXD", "YPF SA", itype="HARD DOLLAR"),
        _on("CP28D", "Cia Gral de Combustibles", itype="DOLLAR LINKED"),
    ]))
    by = {b["ticker"]: b for b in data["bonds"]}
    assert by["YMCXD"]["tipo"] == "HD"
    assert by["CP28D"]["tipo"] == "DL"


def test_on_data_carries_emisor_and_clase():
    """Cada ON lleva emisor (short_name BYMADATA) y clase (serie_clase); la clase
    ausente queda None (no rompe la columna CLASE de la liga)."""
    data = _fetch(_StubState([
        _on("YM39O", "YPF S.A.", clase="Clase XXXIX"),
        _on("YM34O", "YPF S.A."),   # sin serie_clase
    ]))
    by = {b["ticker"]: b for b in data["bonds"]}
    assert by["YM39O"]["emisor"] == "YPF S.A."
    assert by["YM39O"]["clase"] == "Clase XXXIX"
    assert by["YM34O"]["clase"] is None


def test_on_data_has_dias_al_proximo_cupon():
    """El dataset trae días al próximo cupón (primer flujo futuro) — la columna de la
    herramienta usa eso en vez de días al vto."""
    data = _fetch(_StubState([_on("YMCXD", "YPF SA", days=400)]))
    b = data["bonds"][0]
    assert b["dias_cupon"] == 400 and b["prox_cupon"] is not None   # único flujo = today+400
    with TestClient(app) as c:
        js = c.get("/static/js/on.js").text
    assert 'k: "dias_cupon"' in js          # la columna usa días al cupón
    assert 'label: "Días"' not in js or 'label: "Días cupón"' in js  # no quedó "Días" (al vto)


def test_on_data_has_term_and_risk_columns():
    """El dataset trae emisión, vto, cupón, frecuencia, current yield y convexidad,
    y la tabla expone esas columnas."""
    data = _fetch(_StubState([_on("YMCXD", "YPF SA", tir=0.08, md=3.0)]))
    b = data["bonds"][0]
    for k in ("emision", "vto", "cupon", "frec", "cy", "convex"):
        assert k in b
    assert b["frec"] == 2                # payment_frequency default
    assert b["convex"] is not None       # hay tir + flujo → convexidad calculable
    with TestClient(app) as c:
        js = c.get("/static/js/on.js").text
    for col in ('k: "emision"', 'k: "cupon"', 'k: "frec"', 'k: "cy"', 'k: "convex"'):
        assert col in js


def test_on_data_pesos_leg_cy_converts_via_fx():
    """La pata pesos (…O) pasa su precio a USD por MEP/cable antes del Current Yield,
    así su CY coincide con el de la pata USD del mismo bono (no da basura)."""
    mat = date.today() + timedelta(days=400)
    cfs = [Cashflow(date.today() + timedelta(days=200), 0.0, 4.0),   # cupón con interés > 0
           Cashflow(mat, 100.0, 4.0)]

    def mk(ticker, price):
        inst = Instrument(ticker=ticker, short_name="YPF SA", instrument_type="HARD DOLLAR",
                          maturity_date=mat, ley_aplicable="Argentina", cashflows=cfs)
        snap = MarketSnapshot(instrument=inst, price=price, last_update=date.today())
        return InstrumentMetrics(snapshot=snap, tir=0.08, duration=1.0, technical_value=100.0, parity=1.0)

    # MEP=10 → usd_price(…O) = 1000/10 = 100 == price(…D) = 100 → mismo CY
    data = _fetch(_StubState([mk("YPFXO", 1000.0), mk("YPFXD", 100.0)]), fx=_StubFx(10.0))
    by = {b["ticker"]: b for b in data["bonds"]}
    assert by["YPFXO"]["cy"] is not None and by["YPFXD"]["cy"] is not None
    assert abs(by["YPFXO"]["cy"] - by["YPFXD"]["cy"]) < 0.05   # misma CY tras convertir

    # sin fx → la pata pesos no se puede convertir → CY None (no basura)
    data2 = _fetch(_StubState([mk("YPFXO", 1000.0)]), fx=None)
    assert data2["bonds"][0]["cy"] is None


def test_on_data_tir_none_stays_none():
    data = _fetch(_StubState([_on("YMCXD", "YPF SA", tir=None, parity=None)]))
    b = data["bonds"][0]
    assert b["tir"] is None and b["paridad"] is None


def test_on_data_price_required():
    data = _fetch(_StubState([
        _on("YMCXD", "YPF SA", price=109.9),
        _on("AFCHD", "Banco X", price=0.0),    # sin precio → fuera
        _on("BPCVD", "Banco Y", price=None),   # sin precio → fuera
    ]))
    assert [b["ticker"] for b in data["bonds"]] == ["YMCXD"]


def test_on_data_sectors_summary_uses_mep_leg():
    # 3 patas del mismo ON (ARS/MEP/CABLE) → 1 ON canónico (pata MEP), 3 legs.
    data = _fetch(_StubState([
        _on("YMCXO", "YPF SA", price=90000.0),   # ARS
        _on("YMCXD", "YPF SA", price=99.0),       # MEP
        _on("YMCXC", "YPF SA", price=98.0),       # CABLE
    ]))
    assert data["meta"]["n_legs"] == 3
    assert data["meta"]["n_bonds"] == 1
    assert len(data["sectors"]) == 1
    assert data["sectors"][0]["key"] == "Energía / Petróleo & Gas"
    assert data["sectors"][0]["count"] == 1


def test_nav_has_on_button_next_to_bonos():
    import re
    with TestClient(app) as c:
        html = c.get("/on").text
    # Adyacencia Bonos → O.N's, tolerando el whitespace que introdujeron los
    # `{% if has_tab(...) %}` del nav (antes iban pegados en una sola línea).
    assert re.search(r'<a href="/">Bonos</a>\s*<a href="/on">O\.N\'s</a>', html)


def test_on_page_uses_unified_tool():
    """La subpestaña Sectores ahora es la herramienta unificada Sector›Emisor›Título
    (reemplaza liga + heatmap) con el gráfico TIR-vs-MD en un modal."""
    with TestClient(app) as c:
        html = c.get("/on").text
        js = c.get("/static/js/on.js").text
    assert 'id="uni-tool"' in html                       # mount de la herramienta
    assert 'id="uni-chart-backdrop"' in html and 'id="uni-chart"' in html  # modal + canvas
    assert 'id="sec-liga"' not in html and 'id="sec-hm"' not in html       # liga + heatmap fuera
    assert "window.Unified" in js and "Unified.render" in js               # unified.js embebido + usado
    # las tarjetas por sector ahora van en un rail al costado del listado (sin botón de colapsar)
    assert 'id="sec-cards"' in html and 'class="sec-split"' in html
    assert 'id="sec-cards-toggle"' not in html
    assert "function secRenderCards" in js


def test_on_js_wires_live_refresh():
    """on.js debe re-fetchear /on/data en vivo (SSE /stream + intervalo) — si no,
    el panel queda congelado al primer load y las ediciones del ABM / precios no
    se ven sin recargar la página (lo que motivó este fix)."""
    with TestClient(app) as c:
        js = c.get("/static/js/on.js").text
    assert 'new EventSource("/stream")' in js
    assert "onLiveRefresh" in js and "setInterval(" in js
    assert "visibilitychange" in js          # refresca al volver a la pestaña (ediciones ABM)
    assert js.count('fetch("/on/data")') >= 1
    # El botón 🖨️ POSTea a /on/pdf los tickers visibles (filteredList) — con un GET
    # pelado el PDF ignoraba las facetas del sidebar y bajaba el universo HD completo.
    assert "downloadOnPdf" in js and 'fetch("/on/pdf", {' in js
    assert 'method: "POST"' in js and "filteredList().map(" in js


def test_on_css_no_rompe_el_thead_sticky():
    """Invariante de layout: el `thead` de la tabla unificada es `position: sticky`, y
    sticky se ancla al scrollport ANCESTRO más cercano. Como `.uni-panel` ya no scrollea
    (el alto es natural y scrollea la página), dejarle `overflow: hidden` lo convertía
    igual en scrollport → el encabezado se iba de pantalla en vez de quedarse fijo.
    `overflow: clip` recorta para el border-radius sin crear scrollport."""
    with TestClient(app) as c:
        css = c.get("/static/css/on.css").text
    panel = css.split(".uni-panel {", 1)[1].split("}", 1)[0]
    assert "overflow: clip" in panel and "overflow: hidden" not in panel
    thead = css.split("table.uni thead th {", 1)[1].split("}", 1)[0]
    assert "position: sticky" in thead
    # y se ancla DEBAJO del header sticky del sitio (80px, misma constante que .sidebar)
    assert "top: 80px" in thead


def test_on_pdf_endpoint_returns_pdf():
    """GET /on/pdf (universo completo, lo usa el CLI) debe devolver un PDF descargable.
    charts=false lo arma sin los scatter (rápido, sin depender de matplotlib).

    Sin `importorskip`: fpdf2 está en requirements.txt/lock, así que su ausencia es un
    entorno mal instalado y el test tiene que gritarlo — saltearlo dejaba on_pdf.py
    entero sin cobertura y el endpoint tirando 500 en runtime."""
    with TestClient(app) as c:
        r = c.get("/on/pdf?charts=false")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-" and len(r.content) > 2000
    assert "attachment" in r.headers.get("content-disposition", "")


def test_on_pdf_post_honra_los_tickers_del_cliente():
    """El botón 🖨️ POSTea los tickers que el sidebar de facetas dejó visibles: la ruta
    tiene que pasarlos al generador (el GET, en cambio, no filtra por ticker).
    El filtrado en sí lo cubre tests/test_on_pdf.py."""
    import apps.web.on_pdf as on_pdf_mod

    seen = []
    orig = on_pdf_mod.build_on_pdf

    def _spy(data, **kw):
        seen.append(kw)
        return orig(data, **kw)

    on_pdf_mod.build_on_pdf = _spy
    try:
        with TestClient(app) as c:
            r = c.post("/on/pdf", json={"tickers": ["ACMED", "OTRD"], "charts": False})
    finally:
        on_pdf_mod.build_on_pdf = orig
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    assert seen and seen[-1]["tickers"] == ["ACMED", "OTRD"]


def test_on_pdf_post_sin_tickers_equivale_al_get():
    with TestClient(app) as c:
        r = c.post("/on/pdf", json={"charts": False})
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"


def test_on_pdf_sin_fuentes_devuelve_503_no_500():
    """Host sin ninguna TTF (contenedor pelado): 503 con mensaje accionable en vez de
    un 500 con traceback que en el UI se ve sólo como 'HTTP 500'."""
    import apps.web.on_pdf as on_pdf_mod

    def _boom():
        raise FileNotFoundError("No se encontró una fuente TrueType para el PDF.")

    orig = on_pdf_mod.pick_font
    on_pdf_mod.pick_font = _boom
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/on/pdf?charts=false")
    finally:
        on_pdf_mod.pick_font = orig
    assert r.status_code == 503
    assert "TrueType" in r.json()["detail"]


def test_on_pdf_sin_fpdf2_devuelve_503_no_500():
    """Sin la dep opcional el endpoint debe degradar a 503, no romper con
    ModuleNotFoundError (que FastAPI convierte en un 500 opaco)."""
    import sys

    saved = sys.modules.pop("apps.web.on_pdf", None)
    sys.modules["apps.web.on_pdf"] = None    # hace que el `from … import` tire ImportError
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/on/pdf?charts=false")
    finally:
        del sys.modules["apps.web.on_pdf"]
        if saved is not None:
            sys.modules["apps.web.on_pdf"] = saved
    assert r.status_code == 503
    assert "fpdf2" in r.json()["detail"]


def _amort_probe(ticker, face, rate, today):
    """ON amortizing con face residual `face` (capital factor face/100) y TNA `rate`
    (decimal), 2 cupones semestrales. Construye en 2 pasos para que el interés del
    cashflow sea EXACTAMENTE face·rate·dcf con el dcf real del day-count del bono."""
    emission = today - timedelta(days=183)
    c1 = today + timedelta(days=183)
    c2 = today + timedelta(days=366)
    probe = Instrument(ticker=ticker, short_name="X", instrument_type="HARD DOLLAR",
                       maturity_date=c2, emission_date=emission,
                       cashflows=[Cashflow(c1, 0.0, 1.0), Cashflow(c2, face, 1.0)])
    dcf = probe.year_fraction_to(c1, emission)
    interest = face * rate * dcf
    return Instrument(ticker=ticker, short_name="X", instrument_type="HARD DOLLAR",
                      maturity_date=c2, emission_date=emission,
                      cashflows=[Cashflow(c1, 0.0, interest), Cashflow(c2, face, interest)])


def test_current_coupon_normalizes_residual_face():
    """A8: el cupón devenga sobre el nominal vigente (outstanding), no sobre 100.
    Para una ON con face residual < 100 (capital factor < 1), nxt.interest/dcf =
    coupon_rate × outstanding → _current_coupon debe normalizar a face 100 y devolver
    la TNA real (8.8), no el valor escalado (≈5.72)."""
    today = date(2026, 6, 15)
    # face residual 65 (como IRCFO), TNA 8.8% → buggy daría ~5.72
    inst = _amort_probe("IRCFO", 65.0, 0.088, today)
    cup = on_service._current_coupon(inst, today)
    assert round(cup, 2) == 8.80, cup            # TNA real, NO 5.72 escalado
    # no-regresión: face 100 (vr=100) → outstanding 100 → sin cambio
    bullet = _amort_probe("YM00D", 100.0, 0.10, today)
    assert round(on_service._current_coupon(bullet, today), 2) == 10.00


def test_on_js_escapes_editable_text_fields():
    """E1 (XSS): los campos de texto ABM-editables (emisor/clase/ticker) se inyectan
    en innerHTML/title pasando por ON.esc() — un valor con markup no ejecuta JS."""
    with TestClient(app) as c:
        js = c.get("/static/js/on.js").text
    assert "function esc(" in js and "esc: esc," in js     # esc existe y se exporta
    # los sinks de texto crudo ABM-editable van escapados
    for needle in ("ON.esc(b.clase)", "ON.esc(e.name)", "ON.esc(b.ticker)", "ON.esc(em)"):
        assert needle in js, needle


def test_on_js_hydrates_sectors_from_dataset():
    """E3: /on/data emite sectors_meta (espejo de on_classification.SECTORS) y on.js lo
    consume en el boot (ON.syncSectors), con la copia horneada como fallback."""
    from core.domain.on_classification import SECTORS
    data = _fetch(_StubState([_on("YMCXD", "YPF SA")]))
    meta = data["sectors_meta"]
    assert len(meta) == len(SECTORS)
    for s, m in zip(SECTORS, meta):       # mismo orden, mismos key/short/color/icon
        assert (m["key"], m["short"], m["color"], m["icon"]) == (s.key, s.short, s.color, s.icon)
    with TestClient(app) as c:
        js = c.get("/static/js/on.js").text
    assert "ON.syncSectors(d.sectors_meta)" in js          # el cliente consume el payload
    assert "function syncSectors" in js                    # el helper que muta in-place
    assert "window.ON_SECTORS = [" in js                   # copia horneada = fallback


def test_on_tipo_facet_defaults_to_hard_dollar_only():
    """El facet Tipo arranca con SOLO Hard Dollar marcado (DL se prende a mano).
    Se valida el HTML de los checkboxes y el estado inicial del on.js servido."""
    with TestClient(app) as c:
        html = c.get("/on").text
        js = c.get("/static/js/on.js").text
    # checkbox: HD checked, DL NO checked
    assert 'name="tipo" value="HD" checked' in html
    assert 'name="tipo" value="DL" checked' not in html
    # estado inicial del filtro en el JS servido (el motor del filtro)
    assert "tipo: { HD: true, DL: false }" in js


def test_on_data_expone_el_isin():
    """La columna ISIN del panel se alimenta de /on/data. Se asertan el VALOR y el
    caso vacío: con el stub sin ISIN (todo None) el test pasaba aunque on_service
    hardcodeara `"isin": None`, así que no probaba nada."""
    data = _fetch(_StubState([
        _on("YMCXD", "YPF S.A.", isin="USP989MJBV29"),
        _on("YM34O", "YPF S.A."),   # catálogo sin ISIN → None, no rompe la columna
    ]))
    by = {b["ticker"]: b for b in data["bonds"]}
    assert by["YMCXD"]["isin"] == "USP989MJBV29"   # sale el del catálogo, no un fijo
    assert "isin" in by["YM34O"] and by["YM34O"]["isin"] is None
