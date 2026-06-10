"""Enriquecimiento de campos ricos de la ficha técnica BYMA
(core.infrastructure.byma.catalog_enrich): curación pura + persistencia idempotente
en raw_fields['byma']['ficha'] sobre la DB aislada (conftest)."""

from core.infrastructure.byma.catalog_enrich import _curate_ficha, enrich_ficha_meta
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import InstrumentORM

# data[0] real de la ficha de AL30 (recortado).
_AL30_FICHA = {
    "ley": "Nacional", "moneda": "Dólares", "denominacionMinima": 1,
    "formaAmortizacion": "  La amortización se efectuará en TRECE (13) cuotas...  ",
    "interes": "Devengarán intereses...", "montoNominal": 100, "montoResidual": 94,
    "tipoObligacion": "Valores Públicos Nacionales", "tipoGarantia": "Comun",
    "tipoEspecie": "Titulos Publicos", "default": "",
    "fechaEmision": "2020-09-04 00:00:00.0", "fechaVencimiento": "2030-07-09 00:00:00.0",
    "codigoIsin": "ARARGE3209S6", "emisor": "Gobierno Nacional",
}


def test_curate_ficha_extracts_strips_and_truncates_dates():
    cur = _curate_ficha(_AL30_FICHA)
    assert cur["ley"] == "Nacional"
    assert cur["moneda"] == "Dólares"
    assert cur["amortizacion"].startswith("La amortización")   # trim
    assert cur["monto_residual"] == 94
    assert cur["denom_minima"] == 1
    assert cur["fecha_vencimiento"] == "2030-07-09"            # truncada a YYYY-MM-DD
    assert "default" not in cur                                # vacío → descartado
    assert "codigoIsin" not in cur                             # no es campo curado


def _seed_instrument(ticker="AL30", isin="ARARGE3209S6", raw=None):
    init_db()
    with SessionLocal.begin() as s:
        s.merge(InstrumentORM(ticker=ticker, isin=isin, short_name="Bonar 2030",
                              instrument_type="BONAR", raw_fields=raw))


def _make_fetch(seen):
    def f(symbol):
        seen.append(symbol.upper())
        return _AL30_FICHA if symbol.upper() == "AL30" else None
    return f


def test_enrich_ficha_meta_persists_and_is_idempotent():
    _seed_instrument(raw={"byma": {"emisor": "Gobierno Nacional"}})

    seen1: list = []
    assert enrich_ficha_meta(fetch_fn=_make_fetch(seen1)) == 1   # solo AL30 trae ficha
    assert "AL30" in seen1
    with SessionLocal() as s:
        o = s.get(InstrumentORM, "AL30")
        byma = (o.raw_fields or {})["byma"]
        assert byma["emisor"] == "Gobierno Nacional"            # preserva lo previo
        assert byma["ficha"]["ley"] == "Nacional"
        assert byma["ficha"]["monto_residual"] == 94

    # 2ª corrida: AL30 ya tiene 'ficha' → NO se vuelve a pedir (idempotente).
    seen2: list = []
    enrich_ficha_meta(fetch_fn=_make_fetch(seen2))
    assert "AL30" not in seen2


def test_enrich_ficha_meta_skips_rows_without_isin():
    _seed_instrument(ticker="NOISIN", isin=None)
    enrich_ficha_meta(fetch_fn=lambda s: _AL30_FICHA)
    with SessionLocal() as s:
        o = s.get(InstrumentORM, "NOISIN")
        assert (o.raw_fields or {}).get("byma", {}).get("ficha") is None
