"""Ingesta de la CLASE de las ON (scripts/ingest_on_clases.py, ingest_ypf_clases.py).

Cubre los hallazgos del review:
  · la auditoria de cupon comparaba contra `spread_rate` (el spread sobre TAMAR,
    NULL en las 197 ON) en vez del cupon real de `raw_fields["cupon anual %"]`:
    codigo muerto que SIEMPRE reportaba "no hay diferencias" y le dio un OK falso
    al operador;
  · el reporte llamaba "filas descartadas" a filas que SI se escribieron (las de
    vencimiento ilegible), asi que no era auditable;
  · escribir `raw_fields` no puede pisar las otras claves del blob.
"""

import csv

import pytest
from sqlalchemy import select

import scripts.ingest_on_clases as ic
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import InstrumentORM
from scripts.ingest_on_clases import diff_cupon, parse_vto_cupon


# --------------------------------------------------------------------------- #
# C1: auditoria de cupon (el hallazgo critico)
# --------------------------------------------------------------------------- #
def test_parse_vto_cupon_lee_el_step_up_completo():
    ym, cupones = parse_vto_cupon("Sep-2033 (1,50% / 7,00%)")
    assert ym == (2033, 9)
    assert cupones == [1.5, 7.0]


def test_cupon_que_coincide_no_es_diferencia():
    assert diff_cupon(8.25, [8.25]) is None


def test_cupon_distinto_es_diferencia():
    msg = diff_cupon(5.0, [8.25])
    assert msg and "8.25" in msg


def test_sin_cupon_en_el_catalogo_avisa_que_no_se_pudo_auditar():
    """Es el caso que el bug ocultaba: sin cupon cargado no hay nada que comparar,
    y callarlo se leia como 'coincide'."""
    msg = diff_cupon(None, [8.25])
    assert msg and "sin cupon" in msg.lower()


def test_step_up_acepta_cualquier_tramo_declarado_pero_avisa_si_no_es_el_primero():
    """La fuente declara la escalera entera ('1,50% / 7,00%') y el catalogo guarda
    UN cupon: puede tener cargado el vigente o el final. Coincidir con un tramo
    posterior no es error de carga, pero cambia el devengado -> se informa."""
    assert diff_cupon(1.5, [1.5, 7.0]) is None
    msg = diff_cupon(7.0, [1.5, 7.0])
    assert msg and "step-up" in msg.lower()
    assert diff_cupon(3.0, [1.5, 7.0]) is not None   # no coincide con NINGUN tramo


def test_sin_cupones_declarados_no_hay_auditoria():
    assert diff_cupon(8.25, []) is None


def test_tolerancia_por_redondeo_de_la_fuente():
    assert diff_cupon(8.25, [8.26]) is None      # 1bp: redondeo de la fuente
    assert diff_cupon(8.25, [8.50]) is not None  # 25bp: diferencia real


# --------------------------------------------------------------------------- #
# Integracion contra una DB real (C5/C6/C8 + escritura de raw_fields)
# --------------------------------------------------------------------------- #
@pytest.fixture
def catalogo(tmp_db, monkeypatch):
    """Catalogo minimo + guard neutralizado (ya se testea en test_on_scripts_guards)."""
    from core.infrastructure.db.catalog_repository import init_db

    monkeypatch.setattr(ic, "guard_write", lambda tag, force=False: 0)
    init_db()   # tmp_db solo re-bindea el engine; las tablas hay que crearlas
    with SessionLocal() as s:
        s.add_all([
            InstrumentORM(ticker="GYC3O", sheet="Obligaciones_Negociables",
                          raw_fields={"cupon anual %": "8,25", "sector_override": "Energía"}),
            InstrumentORM(ticker="AERBO", ticker_mep="AERBD",
                          sheet="Obligaciones_Negociables", raw_fields=None),
        ])
        s.commit()
    return tmp_db


def _csv(path, filas, columnas):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columnas)
        w.writeheader()
        w.writerows(filas)
    return path


COLS = ["ticker", "ticker_mep", "vencimiento", "clase"]


def _fila(ticker="GYC3O", mep="", vto="08/03/2032", clase="Clase III (3)"):
    return {"ticker": ticker, "ticker_mep": mep, "vencimiento": vto, "clase": clase}


def _raw(ticker):
    with SessionLocal() as s:
        return s.scalars(
            select(InstrumentORM).where(InstrumentORM.ticker == ticker)).one().raw_fields or {}


def test_escribe_la_clase_matcheando_por_ticker_y_por_pata_mep(catalogo):
    ruta = _csv(catalogo / "c.csv", [
        _fila(),
        _fila("AERBD", "AERBD", "15/12/2026", "Clase XI (11)"),
    ], COLS)

    assert ic.main(dry=False, ruta=ruta) == 0
    assert _raw("GYC3O")["serie_clase"] == "Clase III (3)"
    assert _raw("AERBO")["serie_clase"] == "Clase XI (11)"


def test_no_pisa_las_otras_claves_de_raw_fields(catalogo):
    ic.main(dry=False, ruta=_csv(catalogo / "c.csv", [_fila()], COLS))
    rf = _raw("GYC3O")
    assert rf["cupon anual %"] == "8,25"
    assert rf["sector_override"] == "Energía"


def test_el_dry_run_no_escribe(catalogo):
    assert ic.main(dry=True, ruta=_csv(catalogo / "c.csv", [_fila()], COLS)) == 0
    assert "serie_clase" not in _raw("GYC3O")


def test_una_fila_con_vto_ilegible_se_escribe_y_se_reporta_como_no_auditada(
        catalogo, capsys):
    """C6: antes caia en 'filas descartadas' PERO se escribia igual — el reporte
    listaba como salteadas filas que si habian entrado."""
    ruta = _csv(catalogo / "c.csv", [_fila(vto="27/02/20**")], COLS)

    assert ic.main(dry=False, ruta=ruta) == 0
    out = capsys.readouterr().out
    assert _raw("GYC3O")["serie_clase"] == "Clase III (3)"   # se escribio de verdad
    assert "sin auditar" in out.lower()                      # ...y el reporte lo dice
    assert "salteada" not in out.lower()   # y NO se la cuenta como salteada


def test_una_fila_sin_clase_util_se_saltea_de_verdad(catalogo, capsys):
    """La clase no puede ser el propio ticker: fila mal armada -> no se escribe."""
    ruta = _csv(catalogo / "c.csv", [_fila(clase="GYC3O")], COLS)

    assert ic.main(dry=False, ruta=ruta) == 0
    assert "serie_clase" not in _raw("GYC3O")
    assert "salteadas" in capsys.readouterr().out.lower()


def test_es_idempotente(catalogo):
    ruta = _csv(catalogo / "c.csv", [_fila()], COLS)
    ic.main(dry=False, ruta=ruta)
    primero = _raw("GYC3O")
    ic.main(dry=False, ruta=ruta)
    assert _raw("GYC3O") == primero


def test_un_ticker_que_no_esta_en_el_catalogo_se_reporta(catalogo, capsys):
    ruta = _csv(catalogo / "c.csv", [_fila("ZZZ9O", clase="Clase I (1)")], COLS)
    assert ic.main(dry=False, ruta=ruta) == 0
    assert "ZZZ9O" in capsys.readouterr().out


def test_una_sola_query_para_todo_el_csv(catalogo, monkeypatch):
    """C8: el SELECT estaba DENTRO del loop (~170 round-trips, y cada hit arrastraba
    el cronograma por el lazy='selectin'). Ahora se indexa en una pasada."""
    ruta = _csv(catalogo / "c.csv", [
        _fila(),
        _fila("AERBD", "AERBD", "15/12/2026", "Clase XI (11)"),
    ], COLS)

    llamadas = []
    original = ic.indexar_instrumentos
    monkeypatch.setattr(ic, "indexar_instrumentos",
                        lambda s, claves: (llamadas.append(1), original(s, claves))[1])
    ic.main(dry=False, ruta=ruta)
    assert len(llamadas) == 1


# --------------------------------------------------------------------------- #
# C7: el formato YPF (vto+cupon en una sola columna) entra por el MISMO motor.
# --------------------------------------------------------------------------- #
COLS_YPF = ["ticker", "ley", "clase", "vto_cupon"]


def test_el_formato_ypf_escribe_igual_y_audita_el_cupon(catalogo, capsys):
    ruta = _csv(catalogo / "ypf.csv", [
        {"ticker": "GYC3O", "ley": "AR", "clase": "Clase XXXIV (34)",
         "vto_cupon": "Ene-2034 (5,50%)"},
    ], COLS_YPF)

    assert ic.main(dry=False, ruta=ruta, formato=ic.FORMATO_YPF) == 0
    assert _raw("GYC3O")["serie_clase"] == "Clase XXXIV (34)"
    # cupon catalogo 8,25 vs 5,50 declarado: LA diferencia que el bug ocultaba.
    out = capsys.readouterr().out
    assert "8.25" in out and "5.5" in out


def test_el_formato_ypf_matchea_por_la_pata_mep(catalogo):
    """Los tickers de esa fuente son las patas MEP (…D); la fila del catalogo es
    la base (…O)."""
    ruta = _csv(catalogo / "ypf.csv", [
        {"ticker": "AERBD", "ley": "AR", "clase": "Clase XI (11)",
         "vto_cupon": "Dic-2026 (8,00%)"},
    ], COLS_YPF)

    assert ic.main(dry=False, ruta=ruta, formato=ic.FORMATO_YPF) == 0
    assert _raw("AERBO")["serie_clase"] == "Clase XI (11)"


def test_los_dos_formatos_producen_la_misma_fila_normalizada():
    """Lo unico que cambia entre las fuentes es COMO viene el dato, no que se hace
    con el: las dos colapsan en el mismo FilaClase."""
    iamc = ic.FORMATO_IAMC({"ticker": "GYC3O", "ticker_mep": "GYC3D",
                            "vencimiento": "08/03/2032", "clase": "Clase III (3)"})
    ypf = ic.FORMATO_YPF({"ticker": "GYC3O", "clase": "Clase III (3)",
                          "vto_cupon": "Mar-2032 (8,25%)"})
    assert iamc.ticker == ypf.ticker == "GYC3O"
    assert iamc.clase == ypf.clase == "Clase III (3)"
    # misma fecha, distinta granularidad: el IAMC declara el dia, YPF solo el mes
    assert (iamc.vto.year, iamc.vto.month) == ypf.vto_mes == (2032, 3)


def test_el_shim_ypf_delega_en_el_motor_con_su_formato(monkeypatch):
    """`ingest_ypf_clases.py` quedo como shim: mismo comando de siempre."""
    import scripts.ingest_ypf_clases as ypf

    visto = {}
    monkeypatch.setattr(
        ypf, "ingest_clases",
        lambda dry, ruta, formato, force: visto.update(
            dry=dry, ruta=ruta, formato=formato, force=force) or 0)

    assert ypf.main(dry=True, force=True) == 0
    assert visto["ruta"].name == "ypf_clases.csv"
    assert visto["formato"] is ic.FORMATO_YPF
    assert visto["dry"] is True and visto["force"] is True
