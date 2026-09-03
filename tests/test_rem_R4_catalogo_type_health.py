"""Remediación lote R4_catalogo — la señal de salud de los `instrument_type`.

Dos agujeros que el auditor encontró en el fix del hallazgo #6:

1. `audit_orphan_types()` se escribió pero NO ESTABA CABLEADA a nada de la app:
   sus únicos callers eran el script de migración y los tests, así que el operador
   seguía SIN señal en runtime (el hallazgo pedía explícitamente un chequeo de
   salud en el arranque). Ahora `CatalogRepository._load` audita las filas que ya
   tiene en la mano, loguea WARNING y publica el reporte en `type_health`.

2. `_SHEET_DEFAULT_TYPE['OBLIGACIONES_NEGOCIABLES'] = 'HARD DOLLAR'` es un DEFAULT,
   no una verdad: una ON dollar-linked que entre sin `tipo` no queda invisible —
   queda VISIBLE y preciada como hard-dollar (cambia la MONEDA DE PAGO: bug
   financiero silencioso). El riesgo se acota, no se elimina: el default deja
   traza (WARNING por fila) y el inventario de "tipo asumido" sale en el reporte
   de salud junto a los huérfanos.
"""

from datetime import date

import pytest

from core.infrastructure.repositories import (
    AMBIGUOUS_DEFAULT_SHEETS, audit_catalog_types, build_instrument, explicit_type_of,
)

_ON_SHEET = "Obligaciones_Negociables"


def _seed(**kw):
    """Inserta una fila-bono en la DB del test y devuelve su ticker primario."""
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    init_db()
    base = dict(short_name="EMISOR", maturity_date=date(2028, 10, 31),
                emission_date=date(2024, 4, 30), day_count="ACT/365")
    base.update(kw)
    with SessionLocal.begin() as s:
        s.add(InstrumentORM(**base))
    return base["ticker"]


# --------------------------------------------------------------------------- #
# (1) Cableado: la carga del catálogo audita y deja constancia.
# --------------------------------------------------------------------------- #

def test_cargar_el_catalogo_detecta_los_tipos_huerfanos_y_loguea(tmp_db, caplog):
    """El arranque (CatalogRepository._load) tiene que dejar SEÑAL de un bono
    invisible, sin que nadie corra nada a mano."""
    from core.infrastructure.db.catalog_repository import CatalogRepository

    _seed(ticker="BF39O", ticker_mep="BF39D", sheet=_ON_SHEET,
          instrument_type="OBLIGACIONES_NEGOCIABLES", raw_fields={"origen": "IAMC"})
    _seed(ticker="AL30", sheet="Soberanos", instrument_type="BONAR")

    with caplog.at_level("WARNING"):
        repo = CatalogRepository(auto_seed=False)

    assert [e["ticker"] for e in repo.type_health["orphans"]] == ["BF39O"]
    assert repo.type_health["orphans"][0]["tickers"] == ["BF39O", "BF39D"]
    assert any("BF39O" in r.getMessage() and "huérfano" in r.getMessage()
               for r in caplog.records), caplog.text


def test_reload_refresca_la_senal_de_salud(tmp_db):
    """Arreglar el tipo (migración / ABM) + reload → la señal se apaga sola."""
    from core.infrastructure.db.catalog_repository import CatalogRepository
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    _seed(ticker="BPOA8", sheet="Soberanos", instrument_type="SOBERANOS")
    repo = CatalogRepository(auto_seed=False)
    assert [e["ticker"] for e in repo.type_health["orphans"]] == ["BPOA8"]

    with SessionLocal.begin() as s:
        s.get(InstrumentORM, "BPOA8").instrument_type = "BOPREAL"
    repo.reload()
    assert repo.type_health["orphans"] == []


def test_catalogo_sano_no_reporta_nada(tmp_db, caplog):
    """Sin huérfanos ni tipos asumidos, la carga no ensucia el log."""
    from core.infrastructure.db.catalog_repository import CatalogRepository

    _seed(ticker="AL30", sheet="Soberanos", instrument_type="BONAR",
          raw_fields={"tipo": "BONAR"})
    with caplog.at_level("WARNING"):
        repo = CatalogRepository(auto_seed=False)
    assert repo.type_health == {"orphans": [], "defaulted": []}
    assert not [r for r in caplog.records if "catálogo:" in r.getMessage()], caplog.text


def test_audit_orphan_types_sigue_devolviendo_solo_los_huerfanos(tmp_db):
    """Back-compat: el script de migración y la verificación post-migración siguen
    consumiendo la lista plana de huérfanos (no el dict de baldes)."""
    from apps.web.instruments_abm import audit_catalog_health, audit_orphan_types

    _seed(ticker="BF39O", sheet=_ON_SHEET, instrument_type="OBLIGACIONES_NEGOCIABLES")
    _seed(ticker="YMCIO", sheet=_ON_SHEET, instrument_type="HARD DOLLAR",
          raw_fields={"origen": "IAMC"})            # tipo asumido, NO huérfano

    assert [e["ticker"] for e in audit_orphan_types()] == ["BF39O"]
    health = audit_catalog_health()
    assert [e["ticker"] for e in health["orphans"]] == ["BF39O"]
    assert sorted(e["ticker"] for e in health["defaulted"]) == ["BF39O", "YMCIO"]


# --------------------------------------------------------------------------- #
# (2) El default ambiguo deja traza y entra al reporte de salud.
# --------------------------------------------------------------------------- #

def test_la_hoja_on_esta_declarada_como_default_ambiguo():
    """El riesgo aceptado queda ESCRITO en el código, no en un comentario suelto:
    la hoja de ONs admite HARD DOLLAR y DOLLAR LINKED (distinta moneda de pago)."""
    from core.domain.instrument_groups import OBLIGACIONES_NEGOCIABLES

    assert "OBLIGACIONES_NEGOCIABLES" in AMBIGUOUS_DEFAULT_SHEETS
    assert len(OBLIGACIONES_NEGOCIABLES) > 1, OBLIGACIONES_NEGOCIABLES


def test_build_instrument_avisa_cuando_asume_el_tipo_de_la_on(caplog):
    """Una ON sin `tipo` se tipa HARD DOLLAR por default: visible, pero con la
    moneda de pago ASUMIDA. Tiene que quedar traza con el ticker."""
    with caplog.at_level("WARNING"):
        inst = build_instrument({"ticker_ars": "CP39O", "short_name": "CP"}, _ON_SHEET, [])
    assert inst.instrument_type == "HARD DOLLAR"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("CP39O" in m and "ASUMIDO" in m for m in msgs), msgs


def test_build_instrument_no_avisa_si_la_on_declara_el_tipo(caplog):
    """El aviso es por el DEFAULT, no por la hoja: con `tipo` explícito no ensucia."""
    with caplog.at_level("WARNING"):
        inst = build_instrument({"ticker_ars": "CP39O", "tipo": "DOLLAR LINKED"}, _ON_SHEET, [])
    assert inst.instrument_type == "DOLLAR LINKED"
    assert not [r for r in caplog.records if "ASUMIDO" in r.getMessage()], caplog.text


@pytest.mark.parametrize("sheet", ["Dolar_Linked", "Acciones"])
def test_los_defaults_inequivocos_no_avisan(sheet, caplog):
    """`Dolar_Linked` y `Acciones` tienen UN solo tipo posible: ahí el default es
    una verdad, no una suposición — avisar sería ruido puro."""
    with caplog.at_level("WARNING"):
        build_instrument({"ticker_ars": "TZVD7", "short_name": "X"}, sheet, [])
    assert not [r for r in caplog.records if "ASUMIDO" in r.getMessage()], caplog.text


def test_el_reporte_de_salud_lista_las_on_con_tipo_asumido(tmp_db):
    """El guard de tipos no puede detectar 'HARD DOLLAR' como huérfano (es un tipo
    válido), así que el riesgo del default se reporta en su propio balde."""
    from types import SimpleNamespace

    def row(**kw):
        base = dict(ticker="X", ticker_mep=None, ticker_ccl=None, short_name="",
                    sheet=_ON_SHEET, instrument_type="HARD DOLLAR", raw_fields={})
        base.update(kw)
        return SimpleNamespace(**base)

    rows = [
        row(ticker="ASUMIDA", raw_fields={"origen": "IAMC"}),          # sin `tipo`
        row(ticker="DECLARADA", raw_fields={"tipo": "HARD DOLLAR"}),   # con `tipo`
        row(ticker="DECLDL", instrument_type="DOLLAR LINKED",
            raw_fields={"tipo": "DOLLAR LINKED"}),
        row(ticker="AL30", sheet="Soberanos", instrument_type="BONAR", raw_fields={}),
    ]
    health = audit_catalog_types(rows, log=False)
    assert [e["ticker"] for e in health["defaulted"]] == ["ASUMIDA"]
    assert health["orphans"] == []


def test_las_filas_que_perderian_el_tipo_van_primero(tmp_db):
    """Una ON DOLLAR LINKED sin `tipo` en el blob es el caso PELIGROSO: si alguien
    la reconstruye desde raw_fields queda hard-dollar (otra moneda de pago). Va
    arriba del reporte, delante de las que coinciden con el default."""
    from types import SimpleNamespace

    def row(ticker, itype):
        return SimpleNamespace(ticker=ticker, ticker_mep=None, ticker_ccl=None,
                               short_name="", sheet=_ON_SHEET, instrument_type=itype,
                               raw_fields={"origen": "IAMC"})

    health = audit_catalog_types([row("AAAHD", "HARD DOLLAR"), row("ZZZDL", "DOLLAR LINKED")],
                                 log=False)
    assert [e["ticker"] for e in health["defaulted"]] == ["ZZZDL", "AAAHD"]
    assert health["defaulted"][0]["default_type"] == "HARD DOLLAR"   # ≠ su tipo real


def test_el_reporte_de_salud_loguea_el_balde_de_tipos_asumidos(tmp_db, caplog):
    from types import SimpleNamespace

    rows = [SimpleNamespace(ticker="ASUMIDA", ticker_mep=None, ticker_ccl=None,
                            short_name="", sheet=_ON_SHEET,
                            instrument_type="HARD DOLLAR", raw_fields={"origen": "IAMC"})]
    with caplog.at_level("WARNING"):
        audit_catalog_types(rows)
    assert any("ASUMIDA" in r.getMessage() and "SUPOSICIÓN" in r.getMessage()
               for r in caplog.records), caplog.text


@pytest.mark.parametrize("raw,esperado", [
    ({"tipo": "dollar linked"}, "DOLLAR LINKED"),
    ({"clase": " LECAP "}, "LECAP"),
    ({"tipo": ""}, ""),
    ({"tipo": None}, ""),          # el form normaliza '' → None
    ({}, ""),
])
def test_explicit_type_of(raw, esperado):
    """`explicit_type_of` distingue 'la fila DECLARA el tipo' de 'el tipo salió de
    un default' — es lo que separa los dos baldes del reporte."""
    assert explicit_type_of(raw) == esperado
