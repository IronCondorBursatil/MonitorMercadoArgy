"""Lote Z2_cableado — aritmética del reporte de tipos y avisos duplicados.

(4) El WARNING de "tipo ASUMIDO" contaba como "perderían su tipo si se los
    reconstruyera desde raw_fields" a las filas HUÉRFANAS, que ya salen en el otro
    balde del mismo mensaje: el mismo bono inflaba dos números y —peor— el orden lo
    empujaba al tope del reporte, tapando a las filas que sí corren ese riesgo. Una
    huérfana no PIERDE nada al reconstruirse: su tipo actual no lo entiende ningún
    panel, el default se lo arreglaría.

(5) Guardar una ON sin `tipo` por la ABM dejaba el MISMO WARNING dos veces (el guard
    de `save_instrument` y otra vez adentro de `build_instrument`). Un aviso repetido
    se lee como dos filas afectadas.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.infrastructure.repositories import (
    _resolve_instrument_type, audit_catalog_types, build_instrument,
)

_ON_SHEET = "Obligaciones_Negociables"
# Desde la Fase 9 el save NO sintetiza (leía el reloj) y rechaza un bono normal sin
# flujos: los saves de acá van con un bullet mínimo, que no es lo que están probando.
_CF_BULLET = [{"date": "2028-10-31", "amortization": 100, "interest": 0}]


def _row(ticker, itype, sheet=_ON_SHEET, raw=None):
    """Fila-bono ORM mínima (sin `tipo` en el blob = tipo ASUMIDO del default)."""
    return SimpleNamespace(ticker=ticker, ticker_mep=None, ticker_ccl=None,
                           short_name="", sheet=sheet, instrument_type=itype,
                           raw_fields={"origen": "IAMC"} if raw is None else raw)


# El escenario real: una huérfana (tipo = nombre de hoja, de un ingest viejo), una
# ON dollar-linked que SÍ perdería su tipo, y una que coincide con el default.
_ROWS = [_row("AAAORF", "OBLIGACIONES_NEGOCIABLES"),   # huérfana
         _row("ZZZDL", "DOLLAR LINKED"),               # divergente de verdad
         _row("MMMHD", "HARD DOLLAR")]                 # coincide con el default


def _mensaje_de_asumidos(caplog):
    msgs = [r.getMessage() for r in caplog.records if "SUPOSICIÓN" in r.getMessage()]
    assert len(msgs) == 1, msgs
    return msgs[0]


# --------------------------------------------------------------------------- #
# (4) Aritmética del WARNING
# --------------------------------------------------------------------------- #
def test_la_huerfana_no_se_cuenta_como_que_perderia_su_tipo(caplog):
    """Sólo ZZZDL perdería su tipo: AAAORF ya está contada como huérfana y el
    default la ARREGLARÍA, no la rompería."""
    with caplog.at_level("WARNING"):
        audit_catalog_types(_ROWS)
    assert "1 perderían su tipo" in _mensaje_de_asumidos(caplog), caplog.text


def test_el_mensaje_declara_el_solape_con_el_balde_de_huerfanos(caplog):
    """Las dos cuentas del mismo WARNING se solapan (3 asumidos incluyen 1 huérfana):
    decirlo evita leer 3+1 bonos afectados donde hay 3."""
    with caplog.at_level("WARNING"):
        audit_catalog_types(_ROWS)
    msg = _mensaje_de_asumidos(caplog)
    assert "3 bono(s) sin `tipo`" in msg, msg
    assert "1 ya cuenta(n) como huérfano" in msg, msg


def test_el_orden_pone_primero_a_las_que_de_verdad_perderian_el_tipo():
    """La huérfana no puede tapar a la ON dollar-linked al tope del reporte."""
    health = audit_catalog_types(_ROWS, log=False)
    assert [e["ticker"] for e in health["defaulted"]] == ["ZZZDL", "AAAORF", "MMMHD"]


def test_los_baldes_no_cambian_de_contenido():
    """La corrección es de CUENTA y ORDEN: quién está en cada balde no se toca (la
    huérfana sigue siendo, factualmente, una fila con el tipo asumido)."""
    health = audit_catalog_types(_ROWS, log=False)
    assert [e["ticker"] for e in health["orphans"]] == ["AAAORF"]
    assert sorted(e["ticker"] for e in health["defaulted"]) == ["AAAORF", "MMMHD", "ZZZDL"]


def test_sin_huerfanas_la_cuenta_y_el_solape_no_mienten(caplog):
    with caplog.at_level("WARNING"):
        audit_catalog_types([_row("ZZZDL", "DOLLAR LINKED"), _row("MMMHD", "HARD DOLLAR")])
    msg = _mensaje_de_asumidos(caplog)
    assert "1 perderían su tipo" in msg, msg
    assert "huérfano" not in msg, msg          # sin solape no se ensucia el mensaje


# --------------------------------------------------------------------------- #
# (5) Un save del ABM = un solo aviso
# --------------------------------------------------------------------------- #
def _asumidos(caplog):
    return [r.getMessage() for r in caplog.records if "ASUMIDO" in r.getMessage()]


def test_guardar_una_on_sin_tipo_avisa_exactamente_una_vez(tmp_db, caplog):
    from apps.web.instruments_abm import save_instrument

    with caplog.at_level("WARNING"):
        res = save_instrument(_ON_SHEET, {"ticker_ars": "TESTO", "short_name": "X",
                                          "fecha_vencimiento": "2028-10-31",
                                          "fecha_emision": "2024-04-30"},
                                   _CF_BULLET)
    assert res["action"] == "created"
    msgs = _asumidos(caplog)
    assert len(msgs) == 1, msgs
    assert "TESTO" in msgs[0]


def test_el_aviso_no_desaparecio(tmp_db, caplog):
    """Guard del fix de arriba: silenciar el duplicado no puede silenciar la señal."""
    from apps.web.instruments_abm import save_instrument

    with caplog.at_level("WARNING"):
        save_instrument(_ON_SHEET, {"ticker_ars": "TESTO2", "short_name": "X",
                                    "fecha_vencimiento": "2028-10-31"}, _CF_BULLET)
    assert _asumidos(caplog), caplog.text


def test_guardar_una_on_con_tipo_no_avisa(tmp_db, caplog):
    from apps.web.instruments_abm import save_instrument

    with caplog.at_level("WARNING"):
        save_instrument(_ON_SHEET, {"ticker_ars": "TESTO3", "short_name": "X",
                                    "tipo": "DOLLAR LINKED",
                                    "fecha_vencimiento": "2028-10-31"}, _CF_BULLET)
    assert not _asumidos(caplog), caplog.text


def test_el_guard_de_tipo_huerfano_sigue_rechazando_el_save(tmp_db):
    """El pre-chequeo dejó de LOGUEAR, no de validar: un tipo fuera de
    instrument_groups sigue siendo un ValueError antes de tocar la transacción."""
    from apps.web.instruments_abm import save_instrument

    with pytest.raises(ValueError, match="instrument_groups"):
        save_instrument(_ON_SHEET, {"ticker_ars": "TESTO4", "tipo": "INVENTADO"})


@pytest.mark.parametrize("warn,esperado", [(True, 1), (False, 0)])
def test_resolve_instrument_type_respeta_la_perilla_de_aviso(warn, esperado, caplog):
    """La perilla es lo que separa el PRE-CHEQUEO del camino de escritura: el tipo
    resuelto es el mismo en los dos casos."""
    with caplog.at_level("WARNING"):
        itype = _resolve_instrument_type({"ticker": "CP39O"}, _ON_SHEET, "CP39O", warn=warn)
    assert itype == "HARD DOLLAR"
    assert len(_asumidos(caplog)) == esperado, caplog.text


def test_build_instrument_sigue_avisando_por_defecto(caplog):
    """El default es `warn=True`: el camino real (el que escribe la fila) avisa."""
    with caplog.at_level("WARNING"):
        inst = build_instrument({"ticker_ars": "CP39O", "short_name": "CP"}, _ON_SHEET, [])
    assert inst.instrument_type == "HARD DOLLAR"
    assert len(_asumidos(caplog)) == 1, caplog.text
