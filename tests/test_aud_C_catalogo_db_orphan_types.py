"""Auditoría lote C — `instrument_type` huérfano (no pertenece a ningún grupo).

Hallazgo #6: `build_instrument` cae al NOMBRE DE LA HOJA cuando la fila no trae
`tipo`/`clase`. Como todo el read-path (paneles, `_ALL_TYPES`, `on_service`)
filtra por IGUALDAD EXACTA contra `core/domain/instrument_groups`, un tipo como
"OBLIGACIONES_NEGOCIABLES" o "SOBERANOS" deja al bono invisible en TODOS los
paneles (se carga, se guarda, pero nunca se precia ni se muestra).

El vector real reproducido acá es el round-trip del ABM: una fila sembrada por un
script (`ingest_on_iamc_2026_08.py`) cuyos `raw_fields` NO llevan la clave `tipo`
+ un `save_instrument` posterior (el que dispara `backfill_legs_from_universe`)
→ el tipo se recalcula desde el nombre de la hoja y se pierde.
"""

from datetime import date

import pytest

from apps.web.instruments_abm import _type_field_for
from core.domain.instrument_groups import KNOWN_TYPES, orphan_types
from core.infrastructure.repositories import build_instrument


def test_known_types_cubre_todos_los_grupos():
    """Sanity: el set de tipos conocidos no está vacío y contiene los canónicos."""
    for t in ("BONAR", "BOPREAL", "HARD DOLLAR", "DOLLAR LINKED", "LECAP",
              "DOLAR_LINKED", "PURO", "ACCION"):
        assert t in KNOWN_TYPES, t


def test_orphan_types_detecta_los_tipos_de_la_db_viva():
    """Guard: los dos tipos huérfanos medidos en la catalog.db real se detectan."""
    assert orphan_types(["HARD DOLLAR", "OBLIGACIONES_NEGOCIABLES", "SOBERANOS",
                         "ACCION"]) == ["OBLIGACIONES_NEGOCIABLES", "SOBERANOS"]


def test_build_instrument_on_sin_tipo_no_inventa_un_tipo_huerfano():
    """Fila de ON sin la clave `tipo` (raw_fields de los ingests IAMC) → el tipo
    NO puede ser el nombre de la hoja: ningún panel filtra por eso."""
    row = {"ticker_ars": "BF39O", "ticker_mep": "BF39D",
           "short_name": "BANCO BBVA ARGENTINA S.A.", "ley_aplicable": "Argentina"}
    inst = build_instrument(row, "Obligaciones_Negociables", [])
    assert inst is not None
    assert inst.instrument_type != "OBLIGACIONES_NEGOCIABLES"
    assert not orphan_types([inst.instrument_type]), inst.instrument_type
    # El default sensato de la hoja de ONs es hard-dollar (on_catalog.ITYPE).
    assert inst.instrument_type == "HARD DOLLAR"


def test_build_instrument_tipo_vacio_no_produce_el_tipo_none():
    """El form del ABM normaliza '' → None; `row.get('tipo', ...)` devolvía None
    (la clave EXISTE) → itype 'NONE', otro tipo huérfano."""
    row = {"ticker_ars": "XXXO", "tipo": None, "short_name": "X"}
    inst = build_instrument(row, "Obligaciones_Negociables", [])
    assert inst.instrument_type != "NONE"
    assert not orphan_types([inst.instrument_type])


def test_build_instrument_hoja_sin_default_avisa(caplog):
    """Hoja sin mapeo explícito y sin `tipo` → se loguea WARNING con el ticker
    (no puede pasar en silencio)."""
    with caplog.at_level("WARNING"):
        inst = build_instrument({"ticker_ars": "BPOA8"}, "Soberanos", [])
    assert inst is not None
    assert any("BPOA8" in r.getMessage() for r in caplog.records), caplog.text


@pytest.mark.parametrize("itype,sheet,extra", [
    # (1) ON dollar-linked: el default de hoja es HARD DOLLAR, así que sin la
    #     inyección del tipo desde la COLUMNA el round-trip la RE-TIPA en silencio
    #     (pasa a preciarse con la strategy equivocada: paga USD en vez de pesos×FX).
    ("DOLLAR LINKED", "Obligaciones_Negociables",
     {"ticker": "CP39O", "ticker_mep": None}),
    # (2) Soberano: la hoja NO tiene default (BONAR/GLOBAL/BOPREAL son ambiguos) →
    #     sin la inyección el save explota con "tipo 'SOBERANOS' no pertenece…".
    ("BOPREAL", "Soberanos",
     {"ticker": "BPOA8", "ticker_mep": "BPA8D", "ticker_ccl": "BPA8C"}),
    # (3) ON hard-dollar: coincide con el default de hoja, así que NO es
    #     load-bearing — queda como cobertura del camino feliz.
    ("HARD DOLLAR", "Obligaciones_Negociables",
     {"ticker": "BF39O", "ticker_mep": "BF39D"}),
])
def test_abm_round_trip_preserva_el_instrument_type(tmp_db, itype, sheet, extra):
    """REPRO del daño real: fila sembrada por script (raw_fields sin `tipo`) →
    get_instrument() → save_instrument() debe conservar el tipo de la DB.

    Los casos (1) y (2) son los LOAD-BEARING: el `HARD DOLLAR` del caso (3) es
    justo el default de hoja, así que por sí solo el test pasaría igual con el
    fix revertido (era el agujero que dejaba el test original)."""
    from apps.web.instruments_abm import get_instrument, save_instrument
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import CashflowORM, InstrumentORM

    init_db()
    primary = extra["ticker"]
    with SessionLocal.begin() as s:
        orm = InstrumentORM(
            short_name="EMISOR", instrument_type=itype, sheet=sheet,
            day_count="ACT/365",
            maturity_date=date(2028, 10, 31), emission_date=date(2024, 4, 30),
            # raw_fields SIN la clave `tipo`: así los dejan los ingest del IAMC.
            raw_fields={"origen": "IAMC", "ley_aplicable": "Argentina",
                        "cupon_anual_pct": 5.8},
            **extra)
        # Con schedule: desde la Fase 9 el round-trip del ABM lo reenvía tal cual y un
        # bono normal SIN flujos se rechaza (antes el save lo sintetizaba con el reloj).
        orm.cashflows = [CashflowORM(ticker=primary, fecha_pago=date(2028, 10, 31),
                                     amortizacion=100.0, cupon_interes=2.9)]
        s.add(orm)

    payload = get_instrument(primary)
    assert payload is not None
    # El form recibe el tipo (viene de la columna aunque raw_fields no lo traiga).
    tkey = _type_field_for(sheet)
    assert payload["fields"].get(tkey) == itype, payload["fields"]

    save_instrument(payload["sheet"], payload["fields"], payload["cashflows"] or None)

    with SessionLocal() as s:
        orm = s.get(InstrumentORM, primary)
        assert orm.instrument_type == itype, orm.instrument_type
        # y el blob queda con el tipo, para que el PRÓXIMO round-trip no dependa
        # otra vez del fallback.
        assert (orm.raw_fields or {}).get(tkey) == itype


def test_save_instrument_rechaza_un_tipo_que_ningun_panel_muestra(tmp_db):
    """El path ABM no puede persistir un tipo huérfano ni por accidente."""
    from apps.web.instruments_abm import save_instrument

    with pytest.raises(ValueError, match="(?i)tipo"):
        save_instrument("Obligaciones_Negociables",
                        {"ticker_ars": "ZZZ1O", "tipo": "OBLIGACIONES_NEGOCIABLES",
                         "short_name": "Z"})


def test_audit_orphan_types_lista_los_tickers(tmp_db):
    """Guard operable: la auditoría lista los bonos con tipo huérfano."""
    from apps.web.instruments_abm import audit_orphan_types
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    init_db()
    with SessionLocal.begin() as s:
        s.add(InstrumentORM(ticker="BF39O", ticker_mep="BF39D", short_name="BBVA",
                            instrument_type="OBLIGACIONES_NEGOCIABLES",
                            sheet="Obligaciones_Negociables"))
        s.add(InstrumentORM(ticker="AL30", short_name="AL30",
                            instrument_type="BONAR", sheet="Soberanos"))
        s.add(InstrumentORM(ticker="GGAL", short_name="GGAL",
                            instrument_type="ACCION", sheet="Acciones"))

    orphans = audit_orphan_types()
    assert [o["ticker"] for o in orphans] == ["BF39O"]
    assert orphans[0]["instrument_type"] == "OBLIGACIONES_NEGOCIABLES"
    assert orphans[0]["tickers"] == ["BF39O", "BF39D"]
