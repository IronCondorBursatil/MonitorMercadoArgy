"""Novedades del universo — schema, diff puro, agenda y store (spec 2026-09-07)."""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import inspect

from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import configure, get_engine
from tests._clock import ref_date

AR = ZoneInfo("America/Argentina/Buenos_Aires")
HOY = ref_date()


@pytest.fixture
def restore_engine():
    """Restaura el engine de la suite al terminar (mismo patrón que test_catalog_migration)."""
    from config.settings import settings
    yield
    configure(settings.catalog_db)


@pytest.fixture
def base(tmp_path):
    """DB temporal VACÍA con schema al día."""
    from config.settings import settings
    configure(tmp_path / "nov.db")
    init_db()
    try:
        yield
    finally:
        configure(settings.catalog_db)


# ── schema ──────────────────────────────────────────────────────────────────
def test_init_db_crea_universe_novedades_y_migra_byma_catalog(tmp_path, restore_engine):
    """Forward-only: una DB vieja (byma_catalog sin las columnas nuevas, sin la tabla de
    novedades) sale de init_db con todo agregado y la fila previa intacta."""
    configure(str(tmp_path / "old.db"))
    eng = get_engine()
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE byma_catalog (symbol VARCHAR PRIMARY KEY, categoria VARCHAR)")
        conn.exec_driver_sql(
            "INSERT INTO byma_catalog (symbol, categoria) VALUES ('AL30', 'Títulos Públicos')")

    init_db()

    insp = inspect(get_engine())
    cols = {c["name"] for c in insp.get_columns("byma_catalog")}
    assert {"denominacion", "vencimiento", "last_seen"} <= cols
    assert insp.has_table("universe_novedades")
    ncols = {c["name"] for c in insp.get_columns("universe_novedades")}
    assert {"symbol", "first_seen", "source", "categoria", "estado", "updated_at"} <= ncols
    with get_engine().begin() as conn:
        row = conn.exec_driver_sql(
            "SELECT categoria FROM byma_catalog WHERE symbol='AL30'").fetchone()
    assert row is not None and row[0] == "Títulos Públicos"


# ── diff puro ───────────────────────────────────────────────────────────────
from datetime import datetime  # noqa: E402

from core.infrastructure.byma import novedades as nov  # noqa: E402


def _vistos(n: int, extra: dict | None = None) -> dict:
    """n símbolos conocidos (bucket stocks) + extra {symbol: bucket}."""
    d = {f"K{i:03d}": "stocks" for i in range(n)}
    d.update(extra or {})
    return d


def _conocidos(n: int) -> set:
    return {f"K{i:03d}" for i in range(n)}


def test_meta_de_deriva_categoria_moneda_y_ticker_base_como_el_seed():
    m = nov.meta_de("S29E7", "notes")
    assert m["categoria"] == "Títulos Públicos" and m["panel"] == "Letras"
    assert m["security_type"] == "GO" and m["ins_type"] == "BOND"
    assert m["moneda"] == "ARS" and m["ticker_pesos"] == "S29E7"
    d = nov.meta_de("BPOA8D", "bonds")
    assert d["moneda"] == "MEP" and d["ticker_pesos"] == "BPOA8"
    # ON: el seed agrupa las tres patas bajo la pata PESOS completa (AEC2D → AEC2O)
    c = nov.meta_de("YMCXC", "corp")
    assert c["moneda"] == "cable" and c["ticker_pesos"] == "YMCXO"
    assert c["categoria"] == "Obligaciones Negociables"
    assert (nov.meta_de("YMCXO", "corp")["ticker_pesos"]
            == nov.meta_de("YMCXD", "corp")["ticker_pesos"] == "YMCXO")
    # acciones/cedears: misma convención por sufijo que el seed (ALUAD → ALUA, MEP)
    a = nov.meta_de("ALUAD", "stocks")
    assert a["moneda"] == "MEP" and a["ticker_pesos"] == "ALUA"
    assert nov.meta_de("XXX", "")["categoria"] == "Otros"   # bucket desconocido: no se inventa


def test_un_simbolo_visto_que_nadie_conoce_es_nueva_con_su_procedencia():
    diff = nov.clasificar(_vistos(60, {"S29E7": "notes", "D30O6": "notes"}),
                          listados_byma={"S29E7"}, catalogo=_conocidos(60),
                          registradas=set(), pendientes=set(), cargados=set())
    assert diff.rechazo is None and diff.vistos == 62
    assert [f["symbol"] for f in diff.nuevas] == ["D30O6", "S29E7"]
    por = {f["symbol"]: f["source"] for f in diff.nuevas}
    assert por == {"S29E7": "byma", "D30O6": "data912"}
    assert diff.altas_catalogo == diff.nuevas


def test_lo_conocido_no_es_novedad_ni_entra_al_catalogo():
    diff = nov.clasificar(_vistos(60), set(), catalogo=_conocidos(60),
                          registradas=set(), pendientes=set(), cargados=set())
    assert diff.nuevas == [] and diff.altas_catalogo == []


def test_lo_ya_cargado_en_instruments_entra_al_catalogo_pero_no_es_novedad():
    """Un alta hecha a mano de algo que el CSV no tenía: se conoce el símbolo (va a
    byma_catalog para el Universo) pero no hay nada que decidir."""
    diff = nov.clasificar(_vistos(60, {"TSTX1O": "corp"}), set(), catalogo=_conocidos(60),
                          registradas=set(), pendientes=set(), cargados={"TSTX1O"})
    assert [f["symbol"] for f in diff.altas_catalogo] == ["TSTX1O"]
    assert diff.nuevas == []


def test_una_registrada_que_el_catalogo_perdio_vuelve_al_universo_sin_ser_novedad():
    """byma_catalog re-sembrada a mano (force): la especie vuelve al universo, pero la
    decisión que ya está en universe_novedades se respeta."""
    diff = nov.clasificar(_vistos(60, {"ZZZ1": "corp"}), set(), catalogo=_conocidos(60),
                          registradas={"ZZZ1"}, pendientes=set(), cargados=set())
    assert [f["symbol"] for f in diff.altas_catalogo] == ["ZZZ1"]
    assert diff.nuevas == []


# ── regla 4: variantes y una novedad por especie (primera corrida en prod: 4155) ──
def test_sb_y_plazo_especial_son_variantes_pero_no_un_ticker_real_terminado_en_xyz():
    vistos = {"AL30": "bonds", "AL30D": "bonds", "AL30X": "bonds", "AL30Y": "bonds",
              "AL30D.SB": "bonds", "TY30P": "bonds", "TY30X": "bonds",
              "TVPY": "bonds", "ZZ99X": "bonds", "NFLX": "cedears"}
    r = nov._raices(vistos)
    assert nov.es_variante("AL30D.SB", "bonds", r)
    assert nov.es_variante("AL30X", "bonds", r) and nov.es_variante("AL30Y", "bonds", r)
    assert nov.es_variante("TY30X", "bonds", r)          # raíz TY30 compartida con TY30P
    # Renta fija: TODO sufijo X/Y/Z es pata de ámbito, con o sin hermano visto (B2N6X,
    # SE7X en prod: ese día sólo cotizó la pata X). TVPY (único primario así del seed)
    # ya está en el catálogo y nunca llega acá como «nuevo».
    assert nov.es_variante("ZZ99X", "bonds", r) and nov.es_variante("SE7Z", "notes", r)
    assert nov.es_variante("VBC4X", "corp", r)
    assert not nov.es_variante("AL30", "bonds", r) and not nov.es_variante("TZVD8", "bonds", r)
    # Equities: tickers reales de EE.UU. terminan en X/Y/Z (NFLX, SPCX, SKHY): sólo es
    # variante un 5 letras con hermano de raíz (CRESX/IRSAX/VALOX de la 1ª corrida en prod)
    assert not nov.es_variante("NFLX", "cedears", r)
    r2 = nov._raices({"CRES": "stocks", "CRESD": "stocks", "CRESX": "stocks", "SPCX": "cedears",
                      "SKHY": "cedears", "ZZ99X": "cedears"})
    assert nov.es_variante("CRESX", "stocks", r2) and not nov.es_variante("CRESD", "stocks", r2)
    assert not nov.es_variante("SPCX", "cedears", r2) and not nov.es_variante("SKHY", "cedears", r2)
    assert not nov.es_variante("ZZ99X", "cedears", r2)


def test_las_variantes_ni_entran_al_catalogo_ni_son_novedad():
    diff = nov.clasificar(_vistos(60, {"AO29": "bonds", "AO29X": "bonds", "AO29Y": "bonds",
                                       "AO29Z": "bonds", "AO29D.SB": "bonds"}),
                          set(), catalogo=_conocidos(60), registradas=set(),
                          pendientes=set(), cargados=set())
    assert [f["symbol"] for f in diff.altas_catalogo] == ["AO29"]
    assert [f["symbol"] for f in diff.nuevas] == ["AO29"]


def test_una_novedad_por_especie_con_la_pata_pesos_primero():
    """Una ON nueva con sus tres patas es UNA novedad (la pesos); las tres entran al
    catálogo. Si sólo cotizan las patas en dólares, se registra la MEP."""
    diff = nov.clasificar(_vistos(60, {"AEC3C": "corp", "AEC3D": "corp", "AEC3O": "corp",
                                       "ZZ1LD": "corp", "ZZ1LC": "corp"}),
                          set(), catalogo=_conocidos(60), registradas=set(),
                          pendientes=set(), cargados=set())
    assert sorted(f["symbol"] for f in diff.altas_catalogo) == ["AEC3C", "AEC3D", "AEC3O",
                                                                  "ZZ1LC", "ZZ1LD"]
    assert [f["symbol"] for f in diff.nuevas] == ["AEC3O", "ZZ1LD"]


def test_las_patas_nuevas_de_una_especie_cargada_o_registrada_no_son_novedad():
    """AL30C recién listada de un AL30 cargado: va al catálogo (el backfill de patas la
    toma de ahí), no es novedad. Y la pata cable de una ON ya registrada por su pata MEP
    tampoco (`registradas` trae la especie, ver `simbolos_registrados`)."""
    diff = nov.clasificar(_vistos(60, {"AL30C": "bonds", "ZZ1LC": "corp"}), set(),
                          catalogo=_conocidos(60), registradas={"ZZ1LD", "ZZ1LO"},
                          pendientes=set(), cargados={"AL30", "AL30D"})
    assert sorted(f["symbol"] for f in diff.altas_catalogo) == ["AL30C", "ZZ1LC"]
    assert diff.nuevas == []


def test_una_pendiente_que_ya_se_cargo_pasa_a_cargada():
    diff = nov.clasificar(_vistos(60, {"S29E7": "notes"}), set(),
                          catalogo=_conocidos(60) | {"S29E7"}, registradas={"S29E7"},
                          pendientes={"S29E7"}, cargados={"S29E7"})
    assert diff.cargadas == ["S29E7"] and diff.nuevas == []


def test_una_descartada_no_vuelve_a_ser_novedad():
    diff = nov.clasificar(_vistos(60, {"ZZZ1": "corp"}), set(),
                          catalogo=_conocidos(60) | {"ZZZ1"}, registradas={"ZZZ1"},
                          pendientes=set(), cargados=set())
    assert diff.nuevas == [] and diff.cargadas == []


@pytest.mark.parametrize("n, ref, motivo", [
    (0, None, "no tiene símbolos"),
    (10, None, "anémica"),
    (60, 1000, "corte parcial"),
])
def test_una_lectura_rota_se_rechaza_entera_sin_decidir_nada(n, ref, motivo):
    diff = nov.clasificar(_vistos(n, {"S29E7": "notes"} if n else None), set(),
                          catalogo=set(), registradas=set(), pendientes={"S29E7"},
                          cargados={"S29E7"}, ref_vistos=ref)
    assert diff.rechazo and motivo in diff.rechazo
    assert diff.nuevas == [] and diff.altas_catalogo == [] and diff.cargadas == []


def test_el_guard_relativo_no_aplica_sin_corrida_anterior():
    diff = nov.clasificar(_vistos(60), set(), catalogo=_conocidos(60), registradas=set(),
                          pendientes=set(), cargados=set(), ref_vistos=None)
    assert diff.rechazo is None


def test_clasificar_no_muta_lo_que_recibe():
    vistos = _vistos(60, {"S29E7": "notes"})
    copia = dict(vistos)
    nov.clasificar(vistos, set(), catalogo=_conocidos(60), registradas=set(),
                   pendientes=set(), cargados=set())
    assert vistos == copia


# ── agenda 08:00 AR ──────────────────────────────────────────────────────────
def _ar(h, m=0, d=10):
    return datetime(2026, 6, d, h, m, tzinfo=AR)


def test_antes_de_las_8_duerme_hasta_las_8():
    assert nov.proximo_despertar(_ar(7, 30), hecha_hoy=False) == 1800.0


def test_despues_de_las_8_sin_corrida_del_dia_corre_ya():
    assert nov.proximo_despertar(_ar(8, 0), hecha_hoy=False) == 0.0
    assert nov.proximo_despertar(_ar(15, 45), hecha_hoy=False) == 0.0


def test_con_la_corrida_hecha_duerme_hasta_manana_a_las_8():
    assert nov.proximo_despertar(_ar(9, 0), hecha_hoy=True) == 23 * 3600.0
    assert nov.proximo_despertar(_ar(7, 0), hecha_hoy=True) == 25 * 3600.0


def test_categorias_sin_hoja_en_el_abm():
    assert {"Acciones", "Cedears", "Índices", "Totales", "Otros"} <= nov.CATEGORIAS_SIN_HOJA
    assert "Obligaciones Negociables" not in nov.CATEGORIAS_SIN_HOJA
    assert "Títulos Públicos" not in nov.CATEGORIAS_SIN_HOJA


# ── store ───────────────────────────────────────────────────────────────────
from core.infrastructure.db.engine import SessionLocal  # noqa: E402
from core.infrastructure.db.models import BymaCatalogORM, UniverseNovedadORM  # noqa: E402


def _nueva(symbol, source="byma", categoria="Obligaciones Negociables"):
    return {"symbol": symbol, "source": source, "categoria": categoria}


def test_registrar_solo_agrega_y_nunca_pisa_un_estado(base):
    with SessionLocal.begin() as s:
        assert nov.registrar_nuevas_en(s, [_nueva("AAA1O"), _nueva("BBB2O")], hoy=HOY) == ["AAA1O", "BBB2O"]
    assert nov.descartar("AAA1O") is True
    with SessionLocal.begin() as s:
        assert nov.registrar_nuevas_en(s, [_nueva("AAA1O"), _nueva("CCC3O")], hoy=HOY) == ["CCC3O"]
    with SessionLocal() as s:
        assert s.get(UniverseNovedadORM, "AAA1O").estado == "descartada"
        assert s.get(UniverseNovedadORM, "AAA1O").first_seen == HOY.isoformat()
    assert nov.contar_nuevas() == 2


def test_descartar_y_restaurar_son_transiciones_reversibles_y_no_borran(base):
    with SessionLocal.begin() as s:
        nov.registrar_nuevas_en(s, [_nueva("AAA1O")], hoy=HOY)
    assert nov.descartar("aaa1o") is True          # normaliza a upper
    assert nov.descartar("AAA1O") is False         # ya no está en `nueva`
    assert nov.contar_nuevas() == 0
    assert nov.restaurar("AAA1O") is True
    assert nov.contar_nuevas() == 1
    assert nov.descartar("NOEXISTE") is False
    with SessionLocal() as s:
        assert s.get(UniverseNovedadORM, "AAA1O") is not None


def test_marcar_cargadas_solo_toca_las_pendientes(base):
    with SessionLocal.begin() as s:
        nov.registrar_nuevas_en(s, [_nueva("AAA1O"), _nueva("BBB2O")], hoy=HOY)
    nov.descartar("BBB2O")
    assert nov.marcar_cargadas(["aaa1o", "BBB2O", "ZZZ"]) == ["AAA1O"]
    with SessionLocal() as s:
        assert s.get(UniverseNovedadORM, "AAA1O").estado == "cargada"
        assert s.get(UniverseNovedadORM, "BBB2O").estado == "descartada"
        todas, pend = nov.simbolos_registrados(s)
    assert todas == {"AAA1O", "BBB2O"} and pend == set()


def test_simbolos_registrados_trae_tambien_la_especie(base):
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="ZZ1LD", ticker_pesos="ZZ1LO", moneda="MEP",
                             categoria="Obligaciones Negociables"))
        nov.registrar_nuevas_en(s, [_nueva("ZZ1LD")], hoy=HOY)
    with SessionLocal() as s:
        todas, pend = nov.simbolos_registrados(s)
    assert todas == {"ZZ1LD", "ZZ1LO"} and pend == {"ZZ1LD"}


def test_isins_registrados_cruza_con_byma_catalog(base):
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="ZZ1LD", isin="ARTEST000001"))
        s.add(BymaCatalogORM(symbol="ZZ1XD"))
        nov.registrar_nuevas_en(s, [_nueva("ZZ1LD"), _nueva("ZZ1XD"), _nueva("ZZ1QD")], hoy=HOY)
    with SessionLocal() as s:
        assert nov.isins_registrados(s) == {"ARTEST000001"}


def test_descartar_grupo_descarta_solo_las_pendientes_de_esa_categoria(base):
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="AAA1O", categoria="Obligaciones Negociables"))
        nov.registrar_nuevas_en(s, [_nueva("AAA1O"), _nueva("BBB2O"),
                                    _nueva("GGAL2", categoria="Cedears")], hoy=HOY)
    nov.descartar("BBB2O")
    assert nov.descartar_grupo("Obligaciones Negociables") == 1      # BBB2O ya estaba
    assert nov.descartar_grupo("Obligaciones Negociables") == 0
    assert nov.descartar_grupo("No existe") == 0
    assert [f["symbol"] for f in nov.listar("nueva")] == ["GGAL2"]
    assert nov.restaurar("AAA1O") is True                            # reversible


def test_listar_cruza_con_byma_catalog_y_marca_si_es_cargable(base):
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="AAA1O", categoria="Obligaciones Negociables",
                             emisor="ACME S.A.", isin="ARACME000001", moneda="ARS",
                             vencimiento="2028-03-31", denominacion="ON ACME CL 1"))
        nov.registrar_nuevas_en(s, [_nueva("AAA1O"), _nueva("GGAL2", categoria="Acciones")],
                                hoy=HOY)
    filas = {f["symbol"]: f for f in nov.listar("nueva")}
    assert filas["AAA1O"]["emisor"] == "ACME S.A."
    assert filas["AAA1O"]["vencimiento"] == "2028-03-31"
    assert filas["AAA1O"]["cargable"] is True
    assert filas["GGAL2"]["categoria"] == "Acciones"   # sin fila en byma_catalog: la de la novedad
    assert filas["GGAL2"]["cargable"] is False
    grupos = nov.agrupadas("nueva")
    assert [g["categoria"] for g in grupos] == ["Acciones", "Obligaciones Negociables"]


def test_meta_se_lee_y_escribe_en_schema_meta(base):
    assert nov.leer_meta("universe_ultima_corrida") is None
    with SessionLocal.begin() as s:
        nov.escribir_meta_en(s, "universe_ultima_corrida", HOY.isoformat())
        nov.escribir_meta_en(s, "universe_ultimos_vistos", 1234)
    assert nov.leer_meta("universe_ultima_corrida") == HOY.isoformat()
    assert nov.leer_meta("universe_ultimos_vistos") == "1234"
    with SessionLocal.begin() as s:
        nov.escribir_meta_en(s, "universe_ultimos_vistos", 99)     # upsert, no duplica
    assert nov.leer_meta("universe_ultimos_vistos") == "99"
