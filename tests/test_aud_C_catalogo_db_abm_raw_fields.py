"""Auditoría lote C — hallazgo #3: `save_instrument` REEMPLAZA `raw_fields`.

El ABM guarda `raw_fields=normalized` (solo las claves de `SHEET_SCHEMAS[sheet]`)
encima del blob previo: editar un `short_name` borra `byma`/`ficha`, `origen`,
`ley_aplicable` en hojas no-ON y `cupon_anual_pct`. `byma`/`ficha` es cache
re-derivable, pero hasta el próximo arranque `/catalogo/ficha` devuelve vacío y el
buscador de universo pierde la marca `has_ficha`; `ley_aplicable` fuera de la hoja
ON se pierde para siempre (12 filas editables en la DB viva).

El form debe seguir GANANDO sobre sus propias claves (incluido borrarlas cuando el
usuario las vacía) — lo que no puede es tirar las claves que no controla.
"""

from apps.web.instruments_abm import (
    SHEET_SCHEMAS, get_instrument, save_instrument,
)
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import InstrumentORM

_FICHA = {"ley": "Extranjera", "monto_residual": 100.0}


def _post(sheet: str, ticker: str, **overrides) -> dict:
    """Lo que manda el form REAL: SOLO las claves de `SHEET_SCHEMAS[sheet]`
    (`routers/abm.py:244` toma `request.form()`, y el HTML se renderiza desde el
    schema). Prefill con lo que hoy hay en la fila, como hace `/abm/form`."""
    prefill = get_instrument(ticker)["fields"]
    fields = {f["key"]: prefill.get(f["key"], "") or ""
              for f in SHEET_SCHEMAS[sheet]["fields"]}
    fields.update(overrides)
    return fields


def _seed_on():
    init_db()
    with SessionLocal.begin() as s:
        s.add(InstrumentORM(
            ticker="BF39O", ticker_mep="BF39D", short_name="BBVA",
            instrument_type="HARD DOLLAR", sheet="Obligaciones_Negociables",
            isin="AR0914780439", day_count="ACT/365",
            raw_fields={
                "ticker_ars": "BF39O", "ticker_mep": "BF39D",
                "tipo": "HARD DOLLAR", "short_name": "BBVA",
                "origen": "IAMC 2026-08-28 (deuda corporativa)",
                "cupon_anual_pct": 5.8,
                "byma": {"emisor": "BANCO BBVA ARGENTINA S.A.", "ficha": _FICHA},
            }))


def _raw(ticker="BF39O"):
    with SessionLocal() as s:
        return dict(s.get(InstrumentORM, ticker).raw_fields or {})


def test_save_preserva_la_ficha_byma(tmp_db):
    """Editar el emisor no puede tirar el cache `byma`/`ficha`."""
    _seed_on()
    save_instrument("Obligaciones_Negociables",
                    _post("Obligaciones_Negociables", "BF39O",
                          short_name="BBVA ARGENTINA"))

    raw = _raw()
    assert raw.get("byma", {}).get("ficha") == _FICHA, raw.get("byma")
    assert raw["short_name"] == "BBVA ARGENTINA"


def test_save_preserva_las_claves_fuera_del_schema(tmp_db):
    """`origen` y `cupon_anual_pct` no están en SHEET_SCHEMAS: no los manda el form,
    así que el save no puede borrarlos."""
    _seed_on()
    save_instrument("Obligaciones_Negociables",
                    _post("Obligaciones_Negociables", "BF39O"))

    raw = _raw()
    assert raw["origen"] == "IAMC 2026-08-28 (deuda corporativa)"
    assert raw["cupon_anual_pct"] == 5.8


def test_save_preserva_ley_aplicable_en_hojas_sin_ese_campo(tmp_db):
    """`ley_aplicable` NO es campo del form fuera de la hoja ON: hoy se pierde para
    siempre al editar un Soberano/CER/TAMAR."""
    init_db()
    with SessionLocal.begin() as s:
        s.add(InstrumentORM(ticker="AO29", short_name="AO29", instrument_type="BONAR",
                            sheet="Soberanos",
                            raw_fields={"ticker_ars": "AO29", "tipo": "BONAR",
                                        "short_name": "AO29",
                                        "ley_aplicable": "Argentina"}))
    save_instrument("Soberanos", _post("Soberanos", "AO29"))
    assert _raw("AO29")["ley_aplicable"] == "Argentina"


def test_el_form_sigue_ganando_sobre_sus_propias_claves(tmp_db):
    """No-regresión: el valor posteado pisa al viejo, y vaciarlo lo borra."""
    _seed_on()
    save_instrument("Obligaciones_Negociables",
                    _post("Obligaciones_Negociables", "BF39O",
                          ley_aplicable="",           # el usuario lo vacía en el form
                          serie_clase="Clase XXXI"))

    raw = _raw()
    assert raw["serie_clase"] == "Clase XXXI"
    assert not raw.get("ley_aplicable")   # vaciado explícito = borrado


def test_save_consolida_patas_sin_perder_el_blob_de_ninguna(tmp_db):
    """Al consolidar filas-por-pata, el blob resultante hereda de TODAS las filas."""
    init_db()
    with SessionLocal.begin() as s:
        s.add(InstrumentORM(ticker="ZZC1O", short_name="Z", instrument_type="HARD DOLLAR",
                            sheet="Obligaciones_Negociables",
                            raw_fields={"origen": "seed-O"}))
        s.add(InstrumentORM(ticker="ZZC1D", short_name="Z", instrument_type="HARD DOLLAR",
                            sheet="Obligaciones_Negociables",
                            raw_fields={"byma": {"ficha": _FICHA}}))
    save_instrument("Obligaciones_Negociables",
                    {"ticker_ars": "ZZC1O", "ticker_mep": "ZZC1D",
                     "short_name": "Z", "tipo": "HARD DOLLAR"})
    raw = _raw("ZZC1O")
    assert raw["origen"] == "seed-O"
    assert raw["byma"]["ficha"] == _FICHA
