"""ABM router — sólo lectura (GET). El path de escritura reusa instruments_abm
(ya testeado en test_instruments_abm contra archivos temporales); no se testea
escritura acá para no tocar el master Excel real."""

from fastapi.testclient import TestClient

from apps.web.app import app

# Fase 9: el POST de /abm/save lleva el SCHEDULE (la tabla del cajón). El backend ya no
# sintetiza al guardar —leía el reloj, así que el schedule persistido dependía del día
# del alta— y rechaza un alta sin flujos. Estos helpers arman ese POST como lo arma el form.
_CF_BULLET = {"cf_date": ["2027-07-22"], "cf_amort": ["100"], "cf_interest": ["0"]}


def _con_preview(fields: dict, sheet: str = "") -> dict:
    """`fields` + el schedule que el preview propone para ellos (lo que hace el usuario:
    ⟳ Previsualizar → Guardar). `preview_cashflows` devuelve {"cashflows", "nota"}: la
    nota explica la tabla vacía de un tipo de payoff cerrado, al que no se le propone
    schedule porque el save lo descartaría."""
    from apps.web import instruments_abm as abm_store
    cfs = abm_store.preview_cashflows(fields, sheet)["cashflows"]
    assert cfs, "el preview no propuso ningún flujo: el alta no sería guardable"
    return {**fields,
            "cf_date": [c["date"] for c in cfs],
            "cf_amort": [str(c["amortization"]) for c in cfs],
            "cf_interest": [str(c["interest"]) for c in cfs]}


def test_abm_page_renders():
    with TestClient(app) as c:
        r = c.get("/abm")
        assert r.status_code == 200
        assert "ABM" in r.text and "INSTRUMENTOS" in r.text


def test_abm_category_chips_show_loaded_counts():
    """Cada chip de categoría lleva entre () el nº de títulos cargados de esa hoja;
    'Todas' muestra el total."""
    import re
    with TestClient(app) as c:
        html = c.get("/abm").text
    assert re.search(r">Todas \(\d+\)<", html), "falta el conteo en el chip Todas"
    chips = re.findall(r'class="abm-chip[^"]*"[^>]*>([^<]+)</span>', html)
    assert chips, "no se renderizaron chips de categoría"
    assert all(re.search(r"\(\d+\)\s*$", c.strip()) for c in chips), chips


def test_abm_form_for_sheet():
    with TestClient(app) as c:
        r = c.get("/abm/form?sheet=CER")
        assert r.status_code == 200
        assert 'name="ticker_ars"' in r.text and 'name="sheet"' in r.text


def test_abm_on_form_has_categoria_field():
    """El form de ON trae el campo Categoría (sector) para reordenar la ON a mano."""
    with TestClient(app) as c:
        r = c.get("/abm/form?sheet=Obligaciones_Negociables")
    assert r.status_code == 200
    assert 'name="sector_override"' in r.text
    assert "Servicios Financieros" in r.text   # opción del select de categorías
    # campos de denominación (referencia BYMA) para rellenar
    assert 'name="denom_base"' in r.text and 'name="denom_incremento"' in r.text
    assert 'name="valor_nominal"' in r.text


def test_abm_form_unknown_sheet():
    with TestClient(app) as c:
        assert c.get("/abm/form?sheet=NOPE").status_code == 400


def test_abm_form_has_calculator():
    """El editor de un bono cargado trae la calculadora precio↔TIR con selector
    de moneda (default a la pata USD en soberanos)."""
    with TestClient(app) as c:
        r = c.get("/abm/form?sheet=Soberanos&key=AL30")
        assert r.status_code == 200
        assert "/abm/calc" in r.text and 'id="abm-calc-tk"' in r.text


def test_abm_calc_price_to_tir():
    with TestClient(app) as c:
        r = c.post("/abm/calc", data={"ticker": "TZXS8", "price": 89.8})
        assert r.status_code == 200
        assert "TIR" in r.text  # fragmento calc_result (dl de métricas)


def test_abm_save_invalid_shows_error_not_silent():
    """Un save inválido (sin ningún ticker) NO debe fingir éxito: la respuesta
    trae el mensaje de error para que el operador sepa que no se guardó.
    (Antes el router tragaba ValueError/KeyError con `pass` → el usuario veía
    la lista como si hubiera guardado y los cambios 'desaparecían'.)"""
    with TestClient(app) as c:
        r = c.post("/abm/save", data={"sheet": "CER", "short_name": "X",
                                      "fecha_vencimiento": "2030-01-01"})
        assert r.status_code == 200          # sigue devolviendo la lista (HTMX)
        assert "No se guardó" in r.text      # banner de error visible
        assert "ticker" in r.text.lower()    # con el motivo real


def test_abm_save_internal_attribute_error_propagates_not_swallowed():
    """A6: un AttributeError DENTRO de synth_cashflows (bug interno) NO se traga: el
    router lo deja propagar (500) en vez de guardar un alta con cero cashflows en
    silencio. Distingue bug interno (propaga) de input inválido (banner visible,
    cubierto por test_abm_save_invalid_shows_error_not_silent). El except acotado de
    _safe_synth (ValueError/KeyError/TypeError) NO debe ampliarse a Exception."""
    import unittest.mock as mock

    # raise_server_exceptions=False → el 500 se observa como respuesta, no re-raise.
    with TestClient(app, raise_server_exceptions=False) as c:
        # Input VÁLIDO (sintetizaría flujos): el único motivo de fallo es el bug interno.
        fields = {
            "sheet": "Soberanos", "ticker_ars": "TESTBUG", "short_name": "BUG",
            "tipo": "BONAR", "fecha_emision": "2025-01-01",
            "fecha_vencimiento": "2030-01-01", "cupon anual %": "5.0",
            "frecuencia pagos": "2",
        }
        # `build_instrument` (no el synth: desde la Fase 9 el save no sintetiza) es el
        # símbolo interno del camino de escritura.
        with mock.patch("apps.web.instruments_abm.build_instrument",
                        side_effect=AttributeError("bug interno simulado")):
            r = c.post("/abm/save", data=_con_preview(fields))
        assert r.status_code == 500   # propaga: el router solo atrapa ValueError/KeyError


def test_abm_on_list_adaptive_table_shows_fields():
    """La lista del ABM es la tabla de completitud ADAPTATIVA por hoja: para ONs
    muestra los campos (Emisor / Tipo / Ley / …) como columnas, el ticker en pesos y
    la fila filtrable por emisor (data-emisor)."""
    fields = {
        "sheet": "Obligaciones_Negociables",
        "ticker_ars": "TSZ9O", "ticker_mep": "TSZ9D", "ticker_ccl": "",
        "short_name": "TEST DL", "tipo": "DOLLAR LINKED",
        "ley_aplicable": "Argentina",
        "fecha_emision": "2025-07-22", "fecha_vencimiento": "2027-07-22",
        "cupon anual %": "5", "frecuencia pagos": "2",
        "base calculo": "ACT/365", "tipo amortizacion": "bullet",
    }
    with TestClient(app) as c:
        try:
            r = c.post("/abm/save", data={**fields, **_CF_BULLET})
            assert r.status_code == 200 and "No se guardó" not in r.text
            assert "TSZ9O" in r.text                   # ticker pesos
            assert "TEST DL" in r.text                 # emisor (short_name) como columna
            assert "DOLLAR LINKED" in r.text           # tipo como columna
            assert "Argentina" in r.text               # ley como columna
            assert 'data-emisor="test dl"' in r.text    # fila filtrable por emisor
            assert "/abm/form?sheet=Obligaciones_Negociables&key=TSZ9O" in r.text  # botón Editar → cajón
            page = c.get("/abm").text
            assert 'class="abm-seg"' in page            # shell unificado (segmented)
            assert "Universo BYMA" in page and 'id="abm-list"' in page
            assert 'id="abm-chips"' in page             # chips de hoja (cambian las columnas)
        finally:
            c.delete("/abm/instrument/TSZ9O")


def test_abm_list_route_adaptive_by_sheet():
    """El endpoint /abm/list devuelve la tabla de completitud (adaptativa) de una hoja."""
    with TestClient(app) as c:
        r = c.get("/abm/list?sheet=Soberanos")
        assert r.status_code == 200
        assert 'class="abm-t"' in r.text and "Compl." in r.text


def test_abm_universe_route_renders():
    with TestClient(app) as c:
        assert c.get("/abm/universe").status_code == 200


def test_abm_cashflows_editable_roundtrip():
    """El flujo de fondos editable del cajón: POST /abm/cashflows reemplaza los flujos
    (delete+insert) y refresca en caliente."""
    from apps.web.deps import get_repo
    fields = {
        "sheet": "Obligaciones_Negociables",
        "ticker_ars": "TSC1O", "ticker_mep": "TSC1D",
        "short_name": "CF TEST", "tipo": "HARD DOLLAR",
        "fecha_emision": "2025-01-15", "fecha_vencimiento": "2027-01-15",
        "cupon anual %": "8", "frecuencia pagos": "2",
        "base calculo": "ACT/365", "tipo amortizacion": "bullet",
    }
    cf_data = {"cf_ticker": "TSC1O",
               "cf_date": ["2026-07-15", "2027-01-15"],
               "cf_amort": ["0", "100"], "cf_interest": ["4", "4"]}
    with TestClient(app) as c:
        try:
            assert "No se guardó" not in c.post("/abm/save",
                                                 data={**fields, **_CF_BULLET}).text
            r = c.post("/abm/cashflows", data=cf_data)
            assert r.status_code == 200 and "flujos guardados" in r.text
            inst = get_repo().get_instrument_by_ticker("TSC1D")
            assert inst is not None and len(inst.cashflows) == 2
        finally:
            c.delete("/abm/instrument/TSC1O")


def test_abm_save_alta_visible_sin_reiniciar():
    """Un alta por el ABM debe verse en el sistema EN CALIENTE: el save escribe
    SQLite y refresca el repo singleton (el mismo que usa el refresh loop), así
    el ciclo siguiente del motor ya la precia — sin reiniciar el server."""
    from apps.web.deps import get_repo

    fields = {
        "sheet": "Obligaciones_Negociables",
        "ticker_ars": "TST8O", "ticker_mep": "TST8D", "ticker_ccl": "",
        "short_name": "TEST EMISOR", "tipo": "HARD DOLLAR",
        "fecha_emision": "2025-07-22", "fecha_vencimiento": "2027-07-22",
        "cupon anual %": "7.5", "frecuencia pagos": "4",
        "base calculo": "ACT/365", "tipo amortizacion": "bullet",
    }
    with TestClient(app) as c:
        # el camino real del cajón: ⟳ Previsualizar propone el schedule y el submit lo
        # manda. El save NO lo sintetiza (dependía del reloj) — ver Fase 9.
        r = c.post("/abm/save", data=_con_preview(fields))
        assert r.status_code == 200 and "No se guardó" not in r.text
        assert "TST8O" in r.text                       # ya está en la lista
        # visible para el MOTOR sin reinicio: el singleton (el del refresh loop)
        # resuelve ambas patas con sus flujos sintetizados
        repo = get_repo()
        inst = repo.get_instrument_by_ticker("TST8D")
        assert inst is not None and len(inst.cashflows) == 8   # 2y trimestral
        # y el form del ABM prefillea desde la DB (round-trip completo)
        f = c.get("/abm/form?sheet=Obligaciones_Negociables&key=TST8O")
        assert "TST8D" in f.text
        # limpieza: la baja también refresca en caliente
        c.delete("/abm/instrument/TST8O")
        assert repo.get_instrument_by_ticker("TST8D") is None
