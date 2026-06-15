"""Tests del catálogo de Obligaciones Negociables (ONs hard-dollar …D).

Cubre el mapeo …O→…D, la construcción de Instruments (tipo/categoría/day-count/
cashflows) y la validación de VT contra el informe BYMA/IAMC (independiente del
precio), más el alta del panel en el router.
"""

from datetime import date

from core.domain.models import MarketSnapshot
from core.domain.services import FinancialEngine
from core.infrastructure.on_catalog import (
    CATEGORY, DAY_COUNT, ITYPE, build_instruments, to_d_ticker,
)

SETTLE = date(2026, 5, 28)


def test_o_to_d_ticker_mapping():
    assert to_d_ticker("VSCXO") == "VSCXD"
    assert to_d_ticker("VE32P") == "VE32D"
    assert to_d_ticker("AL30D") == "AL30D"  # ya es D → sin cambio


def test_build_instruments_shape_and_metadata():
    insts = build_instruments()
    assert len(insts) >= 36
    for i in insts:
        assert i.instrument_type == ITYPE
        assert i.category == CATEGORY
        assert i.day_count in (DAY_COUNT, "30/360")  # default ACT/365; override por col `base`
        assert i.ticker[-1] in ("O", "D")  # primaria: …O (pesos) o …D si no hay …O
        assert i.cashflows, f"{i.ticker} sin cashflows"
        assert i.cashflows[-1].date == i.maturity_date  # último flujo al vto
        # el capital total amortizado = VR (sum de amortizaciones)
        assert sum(cf.amortization for cf in i.cashflows) > 0


def test_per_row_base_override():
    """La col `base` del CSV pisa el day-count default (ACT/365). Telecom Clase 24
    (TLCPO) es 30/360; YPF YM34O (sin base) queda ACT/365."""
    by_t = {i.ticker: i for i in build_instruments()}
    assert by_t["TLCPO"].day_count == "30/360"
    assert by_t["TLCPO"].ley_aplicable == "NY"
    assert by_t["YM34O"].day_count == DAY_COUNT  # sin override → ACT/365


def test_row_legs_consolidates_existing_currency_legs():
    from core.infrastructure.on_catalog import row_legs
    assert row_legs({"ticker_o": "VSCXO", "legs": "ODC"}) == ["VSCXO", "VSCXD", "VSCXC"]
    assert row_legs({"ticker_o": "TTCDO", "legs": "OD"}) == ["TTCDO", "TTCDD"]
    assert row_legs({"ticker_o": "OZC6O", "legs": "D"}) == ["OZC6D"]
    assert row_legs({"ticker_o": "FOO9O", "legs": ""}) == ["FOO9D"]  # fallback …D


def test_bullet_repays_full_face_at_maturity():
    # MGCRO (primaria): bullet (1 cuota), VR 100 → 100 de amortización al vto.
    inst = next(i for i in build_instruments() if i.ticker == "MGCRO")
    amorts = [cf for cf in inst.cashflows if cf.amortization > 0]
    assert len(amorts) == 1 and amorts[0].date == inst.maturity_date
    assert round(amorts[0].amortization, 6) == 100.0


def test_partial_amortizer_residual_matches_vr_at_settle():
    # PNDCO: VR 40, sólo las cuotas remanentes (>settle) → residual hoy = 40.
    inst = next(i for i in build_instruments() if i.ticker == "PNDCO")
    residual = sum(cf.amortization for cf in inst.cashflows if cf.date > SETTLE)
    assert round(residual, 4) == 40.0


def test_computed_vt_matches_report_within_tolerance():
    # VT (valor técnico) = capital residual + intereses corridos, independiente del
    # precio. Validar contra el informe valida cupón, frecuencia, emisión y VR.
    by_ticker = {i.ticker: i for i in build_instruments()}
    expected = {"VSCXO": 101.1, "YM34O": 103.0, "MGCRO": 100.3,
                "DNCAO": 100.8, "RUCDO": 104.7, "PNDCO": 40.3}
    for tk, vt_ref in expected.items():
        snap = MarketSnapshot(instrument=by_ticker[tk], price=None)
        vt = FinancialEngine.calculate_technical_value(snap, indices_provider=None, ref_date=SETTLE)
        assert abs(vt - vt_ref) < 0.15, f"{tk}: VT {vt:.2f} vs informe {vt_ref}"


def test_panel_registered():
    from apps.web.routers.panels import PANELS, PANEL_ORDER
    assert "obligaciones_negociables" in PANELS
    assert "obligaciones_negociables" in PANEL_ORDER
    _title, types, _cols = PANELS["obligaciones_negociables"]
    assert types == {"HARD DOLLAR", "DOLLAR LINKED"}


def test_abm_on_sheet_registered_with_ticker_slots():
    # La hoja debe figurar en el ABM (dropdown + chips) y traer los slots de
    # ticker por moneda (prepend multi-ticker), con tipo fijo a ON.
    from apps.web.instruments_abm import SHEET_SCHEMAS
    assert "Obligaciones_Negociables" in SHEET_SCHEMAS
    flds = SHEET_SCHEMAS["Obligaciones_Negociables"]["fields"]
    keys = [f["key"] for f in flds]
    assert "ticker_ars" in keys and "ticker" not in keys
    assert next(f for f in flds if f["key"] == "tipo")["options"] == ["HARD DOLLAR", "DOLLAR LINKED"]


def test_on_form_builds_on_instrument_with_defaults():
    # Alta vía ABM: build_instrument infiere tipo ON → categoría + day-count 30/360.
    from core.infrastructure.repositories import build_instrument
    from core.domain.on_cashflows import build_on_cashflows
    cfs = build_on_cashflows(date(2025, 11, 14), date(2037, 11, 14), 0.078, 2)
    row = {"ticker_ars": "MGCRD", "short_name": "PAMPA", "tipo": "HARD DOLLAR",
           "fecha_emision": "2025-11-14", "fecha_vencimiento": "2037-11-14",
           "cupon anual %": "7.8", "frecuencia pagos": "2"}  # base calculo vacío a propósito
    inst = build_instrument(row, "Obligaciones_Negociables", cfs)
    assert inst.instrument_type == "HARD DOLLAR"
    assert inst.category == "Obligaciones Negociables"
    assert inst.day_count == "ACT/365"  # real/365, default ON


def test_abm_raw_fields_prefill_keys():
    # raw_fields debe usar las keys del schema → el form de edición prefilea.
    from core.infrastructure.on_catalog import abm_raw_fields, load_rows
    r = next(x for x in load_rows() if x["ticker_o"] == "PNDCO")  # amortizer, VR 40
    f = abm_raw_fields(r)
    assert f["short_name"] and f["tipo"] == "HARD DOLLAR" and f["base calculo"] == "ACT/365"
    assert f["tipo amortizacion"] == "amortizing"   # label (display/filtro), no driver del synth
    # Los campos que sintetizaban estructura irregular/amortizing se retiraron del schema:
    # el cashflow va EXPLÍCITO (build_on_cashflows usa vr/prox/cuotas del CSV directo).
    for k in ("capital factor", "amort inicio", "amort cantidad", "prox_cupon"):
        assert k not in f
