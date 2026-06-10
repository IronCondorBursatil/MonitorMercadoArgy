"""core/domain/fci/dataset.py — bucketeo mensual de flujos + ensamblado del dataset."""

from datetime import date

from core.domain.fci.dataset import build_fci_dataset, monthly_net_flows


def test_monthly_net_flows_buckets_by_calendar_month():
    daily = {date(2026, 6, 3): 100.0, date(2026, 6, 20): 50.0, date(2026, 5, 15): -30.0}
    out, has = monthly_net_flows(daily, "2026-06-04", n=12)
    assert has is True and len(out) == 12
    assert out[-1] == 150       # junio: 100 + 50
    assert out[-2] == -30       # mayo
    assert sum(out[:-2]) == 0   # meses anteriores sin flujo


def test_monthly_net_flows_empty():
    out, has = monthly_net_flows({}, "2026-06-04")
    assert has is False and out == [0] * 12


def _parsed(fid, cid, fondo, clase):
    return {
        "fondo_id": fid, "clase_id": cid, "fondo_nombre": fondo, "clase_nombre": clase,
        "tipo_renta": "Mercado de Dinero", "moneda": "Peso Argentina", "sociedad": "Soc",
        "depositaria": "Dep", "tipo_dinero": "Clásico", "dias_liquidacion": 0,
        "region": "Argentina", "horizonte": "Corto Plazo", "duration": "Menor o Igual a 1 Año",
        "objetivo": "obj", "inicio": "2020-01-01", "fee_admin": 0.5, "fee_in": 0.0,
        "fee_out": 0.0, "inversion_minima": 1000, "ticker_isin": None, "ticker_bloomberg": None,
        "vcp": 100.0, "fecha_valor": "2026-06-04",
        "rend": {p: ({"tna": 18.0, "directo": 1.5} if p == "mes_1" else
                     {"tna": 30.0, "directo": 30.0} if p == "meses_12" else
                     {"tna": None, "directo": None})
                 for p in ("dias_7", "mes_1", "dias_90", "dias_180", "ytd", "meses_12")},
    }


def test_build_fci_dataset_shape_and_contract():
    parsed = [_parsed(1, 11, "Fima Premium", "Fima Premium - Clase A"),
              _parsed(1, 12, "Fima Premium", "Fima Premium - Clase B")]
    macro = {"mep": {}, "cer": {}, "mep_now": 1255.0}
    ds = build_fci_dataset(parsed, aum_index={}, macro=macro,
                           flows_lookup=lambda f: {}, hist_lookup=lambda f: {},
                           fecha_base="2026-06-04", generated_at="2026-06-04T20:00:00")
    meta = ds["meta"]
    for k in ("fecha_base", "periods", "period_labels", "cat_order", "subs_by_cat",
              "hist_axis", "macro", "n_total", "n_shown", "n_aum_real", "flows_real", "sources"):
        assert k in meta
    assert len(meta["hist_axis"]) == 120
    assert meta["flows_real"] is False           # sin store → 'acumulando'
    assert ds["funds"] and len(ds["funds"]) == 1  # 2 clases → 1 fondo
    f = ds["funds"][0]
    assert f["n_clases"] == 2 and len(f["clases"]) == 2
    assert "compo" not in f                       # composición fuera
    assert len(f["flows"]) == 12 and f["flows_real"] is False
    assert f["hist"] is not None and f["hist_real"] is False   # reconstruido
    assert f["sub"] == "MONEY MARKET ARS CLÁSICO"
