"""core/domain/fci/derive.py — subcategoría, buckets, AUM join, unificación de clases."""

from core.domain.fci.derive import (
    build_aum_index, category_bucket, ccy, dur_years, lookup_aum, norm, settle,
    subcategoria, unify_classes,
)


def test_norm_ccy_settle_dur():
    assert norm("Dinámico") == "dinamico"
    assert ccy("Peso Argentina") == "ARS"
    assert ccy("Dolar Estadounidense") == "USD"
    assert settle(0) == "T+0" and settle(1) == "T+1" and settle(99999) == "Cerrado"
    assert settle(None) == "—"
    assert dur_years("Menor o Igual a 1 Año") == 1.0
    assert dur_years("No Registrada") is None


def test_category_bucket():
    assert category_bucket("Mercado de Dinero") == "Mercado de Dinero"
    assert category_bucket("PyMes") == "Otros"
    assert category_bucket("Infraestructura") == "Otros"


def test_subcategoria_money_market():
    assert subcategoria("Mercado de Dinero", "ARS", "Clásico", None, 0, "X", "", "Argentina") \
        == "MONEY MARKET ARS CLÁSICO"
    assert subcategoria("Mercado de Dinero", "ARS", "Dinámico", None, 0, "X", "", "") \
        == "MONEY MARKET ARS DINÁMICO"
    assert subcategoria("Mercado de Dinero", "USD", None, None, 0, "X", "", "") == "MONEY MARKET USD"


def test_subcategoria_renta_fija_keywords_and_duration():
    assert subcategoria("Renta Fija", "ARS", None, 3.0, 1, "Delta CER Plus", "", "") == "RENTA FIJA CER"
    assert subcategoria("Renta Fija", "ARS", None, 3.0, 1, "Lecaps Plus", "", "") == "RENTA FIJA LECAP / TASA FIJA"
    assert subcategoria("Renta Fija", "ARS", None, 1.0, 1, "Fondo Dólar Linked", "", "") == "RENTA FIJA DÓLAR LINKED"
    assert subcategoria("Renta Fija", "USD", None, 0.5, 1, "Ahorro USD", "", "") == "RENTA FIJA USD CORTO PLAZO"
    assert subcategoria("Renta Fija", "USD", None, 3.0, 1, "Renta USD", "", "") == "RENTA FIJA USD"
    assert subcategoria("Renta Fija", "ARS", None, 0.5, 0, "T+1 fund", "", "") == "RENTA FIJA T+1"
    assert subcategoria("Renta Fija", "ARS", None, 3.0, 1, "Discrecional", "", "") == "RENTA FIJA ARS DISCRECIONAL"


def test_subcategoria_rv_region():
    assert subcategoria("Renta Variable", "ARS", None, None, 1, "Acciones", "", "Argentina") == "RENTA VARIABLE ARGENTINA"
    assert subcategoria("Renta Variable", "ARS", None, None, 1, "Global Eq", "", "Global") == "RENTA VARIABLE GLOBAL / LATAM"


def test_aum_index_and_lookup():
    idx = build_aum_index([
        {"fondo": "Alpha Pesos - Clase A", "patrimonio": 1000, "ccp": 10},
        {"fondo": "Beta", "patrimonio": 500, "ccp": 5},
    ])
    # exact 'fondo - clase'
    v, real = lookup_aum(idx, "Alpha Pesos", "Alpha Pesos - Clase A")
    assert real and v["aum"] == 1000
    # base name
    v, real = lookup_aum(idx, "Beta", "Beta - Clase X")
    assert real and v["aum"] == 500
    # prefijo
    v, real = lookup_aum(idx, "Alpha Pesos", "otra")
    assert real and v["aum"] == 1000
    # sin match
    v, real = lookup_aum(idx, "Zeta", "Zeta A")
    assert v is None and real is False


def _parsed(fid, cid, fondo, clase, moneda, mes1_tna, vcp, **extra):
    rec = {
        "fondo_id": fid, "clase_id": cid, "fondo_nombre": fondo, "clase_nombre": clase,
        "tipo_renta": "Mercado de Dinero", "moneda": moneda, "sociedad": "Soc",
        "depositaria": "Dep", "tipo_dinero": "Clásico", "dias_liquidacion": 0,
        "region": "Argentina", "horizonte": "Corto Plazo", "duration": "Menor o Igual a 1 Año",
        "objetivo": "obj", "inicio": "2020-01-01", "fee_admin": 0.5, "fee_in": 0.0,
        "fee_out": 0.0, "inversion_minima": 1000, "ticker_isin": None, "ticker_bloomberg": None,
        "vcp": vcp, "fecha_valor": "2026-06-04",
        "rend": {p: {"tna": None, "directo": None} for p in
                 ("dias_7", "mes_1", "dias_90", "dias_180", "ytd", "meses_12")},
    }
    rec["rend"]["mes_1"] = {"tna": mes1_tna, "directo": 1.5}
    rec.update(extra)
    return rec


def test_unify_classes_groups_and_picks_main_by_aum():
    parsed = [
        _parsed(1, 11, "Fima Premium", "Fima Premium - Clase A", "Peso Argentina", 16.0, 100.0),
        _parsed(1, 12, "Fima Premium", "Fima Premium - Clase B", "Peso Argentina", 17.0, 200.0),
        _parsed(2, 21, "Beta MM", "Beta MM", "Peso Argentina", 20.0, 50.0),
    ]
    idx = build_aum_index([
        {"fondo": "Fima Premium - Clase A", "patrimonio": 1000, "ccp": 10},
        {"fondo": "Fima Premium - Clase B", "patrimonio": 3000, "ccp": 30},
    ])
    funds = unify_classes(parsed, idx)
    assert len(funds) == 2
    fima = next(f for f in funds if f["fid"] == 1)
    assert fima["n_clases"] == 2
    assert fima["cid"] == 12               # main = mayor AUM (Clase B = 3000)
    assert fima["aum"] == 4000             # suma de AUMs únicos (1000 + 3000)
    assert fima["sub"] == "MONEY MARKET ARS CLÁSICO"
    assert "compo" not in fima             # composición no se incluye
    beta = next(f for f in funds if f["fid"] == 2)
    assert beta["aum"] is None and beta["aum_real"] is False   # sin match → honesto
