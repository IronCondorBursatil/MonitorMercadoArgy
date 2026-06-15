"""Filtro synth-vs-explícito del loader scripts/load_bond.py: si el synth reproduce el
cashflow de la imagen → carga por synth (params); si diverge → carga explícito."""

import pytest

from config.settings import settings
from core.infrastructure.db import engine as db_engine
from core.infrastructure.db.catalog_repository import ingest_from_excel

from scripts.load_bond import load_bond, reconcile_synth_vs_explicit, synth_schedule


@pytest.fixture
def abm_db(tmp_path):
    db_engine.configure(tmp_path / "load_bond.db")
    try:
        ingest_from_excel(str(settings.master_xlsx))
        yield
    finally:
        db_engine.configure(settings.catalog_db)


_FIELDS = {
    "ticker_ars": "ZZLB0", "short_name": "TEST LOADER SA", "tipo": "HARD DOLLAR",
    "ley_aplicable": "Argentina",
    "fecha_emision": "2025-01-15", "fecha_vencimiento": "2027-01-15",
    "cupon anual %": "8", "frecuencia pagos": "2",
    "base calculo": "ACT/365", "tipo amortizacion": "bullet",
}


def _img_from_synth(fields, *, bump_idx=None, bump_int=0.0):
    """Construye el 'cashflow de la imagen' a partir del synth; opcionalmente perturba
    un flujo (simula un no-sintetizable: stub que el synth no genera)."""
    rows = []
    for i, (d, a, it) in enumerate(synth_schedule(fields)):
        if i == bump_idx:
            it = it + bump_int
        rows.append({"date": d.isoformat(), "amortization": a, "interest": it})
    return rows


def test_reconcile_match_picks_synth(abm_db):
    img = _img_from_synth(_FIELDS)
    match, report = reconcile_synth_vs_explicit(_FIELDS, img)
    assert match is True and "SYNTH" in report


def test_reconcile_mismatch_reports_diff(abm_db):
    img = _img_from_synth(_FIELDS, bump_idx=0, bump_int=1.5)  # stub que el synth no devenga
    match, report = reconcile_synth_vs_explicit(_FIELDS, img)
    assert match is False and "EXPLÍCITO" in report


def test_load_bond_synth_when_image_matches(abm_db):
    img = _img_from_synth(_FIELDS)
    res = load_bond("Obligaciones_Negociables", _FIELDS, explicit_cfs=img, backup=False)
    assert res["mode"] == "synth"
    assert res["ticker"] == "ZZLB0" and res["schedule"]   # quedó cargado con flujos


def test_load_bond_explicit_when_image_diverges(abm_db):
    img = _img_from_synth(_FIELDS, bump_idx=0, bump_int=1.5)
    res = load_bond("Obligaciones_Negociables", _FIELDS, explicit_cfs=img, backup=False)
    assert res["mode"] == "explicit"
    # el flujo perturbado quedó cargado tal cual (el synth no lo habría generado)
    first = sorted(res["schedule"], key=lambda r: r["date"])[0]
    assert float(first["interest"]) == pytest.approx(
        sorted(synth_schedule(_FIELDS))[0][2] + 1.5, abs=1e-6)


def test_load_bond_synth_only_without_image(abm_db):
    res = load_bond("Obligaciones_Negociables", _FIELDS, backup=False)
    assert res["mode"] == "synth" and res["schedule"]
