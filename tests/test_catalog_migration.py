"""Migración de schema del catálogo: debe ser FORWARD-ONLY (ALTER aditivo),
NUNCA destructiva. Las altas de la ABM (ON/Acciones editadas a mano) viven SOLO
en la DB y son la fuente de verdad — un `drop_all` ante drift de schema las
borraría irreversiblemente. Estos tests blindan ese invariante (hallazgo C1)."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect

from core.infrastructure.db.engine import configure, get_engine
from core.infrastructure.db.catalog_repository import init_db, get_schema_version, CURRENT_SCHEMA_VERSION


@pytest.fixture
def restore_engine():
    """Guarda el engine vigente (la catalog.db temporal del conftest) y lo
    restaura al terminar, para no contaminar otros tests del proceso."""
    from config.settings import settings
    yield
    configure(settings.catalog_db)


def _make_old_schema_db(db_path) -> None:
    """Crea una `instruments` con el schema VIEJO (sin las columnas nuevas del
    ABM: sheet/raw_fields/ticker_mep/ticker_ccl/isin) y mete una fila tipo ABM."""
    eng = configure(str(db_path))
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE instruments ("
            "  ticker VARCHAR PRIMARY KEY,"
            "  short_name VARCHAR,"
            "  instrument_type VARCHAR,"
            "  payment_frequency INTEGER,"
            "  day_count VARCHAR"
            ")"
        )
        conn.exec_driver_sql(
            "INSERT INTO instruments (ticker, short_name, instrument_type, payment_frequency, day_count)"
            " VALUES ('TESTON', 'Test ON (alta ABM)', 'ON', 2, '30/360')"
        )


def test_schema_drift_preserves_abm_rows(tmp_path, restore_engine):
    """init_db() ante columnas faltantes debe MIGRAR (ALTER), preservando la fila."""
    _make_old_schema_db(tmp_path / "catalog_old.db")

    init_db()

    eng = get_engine()
    cols = {c["name"] for c in inspect(eng).get_columns("instruments")}
    assert {"sheet", "raw_fields", "ticker_mep", "ticker_ccl", "isin"} <= cols, \
        "init_db() no agregó las columnas nuevas"
    with eng.begin() as conn:
        row = conn.exec_driver_sql(
            "SELECT ticker, short_name FROM instruments WHERE ticker='TESTON'"
        ).fetchone()
    assert row is not None, "REGRESIÓN C1: init_db() borró la fila ABM (drop_all destructivo)"
    assert row[1] == "Test ON (alta ABM)"


def test_schema_drift_preserves_existing_cashflows(tmp_path, restore_engine):
    """La migración no debe tocar la tabla cashflows ni sus filas."""
    eng = configure(str(tmp_path / "catalog_cf.db"))
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE instruments ("
            "  ticker VARCHAR PRIMARY KEY, short_name VARCHAR, instrument_type VARCHAR,"
            "  payment_frequency INTEGER, day_count VARCHAR)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE cashflows ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT, ticker VARCHAR,"
            "  fecha_pago DATE, amortizacion FLOAT, cupon_interes FLOAT)"
        )
        conn.exec_driver_sql("INSERT INTO instruments (ticker) VALUES ('TESTON')")
        conn.exec_driver_sql(
            "INSERT INTO cashflows (ticker, fecha_pago, amortizacion, cupon_interes)"
            " VALUES ('TESTON', '2030-01-01', 100.0, 5.0)"
        )

    init_db()

    eng = get_engine()
    with eng.begin() as conn:
        n = conn.exec_driver_sql("SELECT COUNT(*) FROM cashflows WHERE ticker='TESTON'").scalar()
    assert n == 1, "REGRESIÓN C1: la migración perdió cashflows"


def test_init_db_stamps_schema_version(tmp_path, restore_engine):
    """init_db() debe registrar la versión de schema vigente (punto de control para
    backups y futuras migraciones de datos)."""
    configure(str(tmp_path / "catalog_ver.db"))

    init_db()

    assert get_schema_version() == CURRENT_SCHEMA_VERSION
    assert CURRENT_SCHEMA_VERSION >= 1


def test_schema_version_unknown_on_fresh_db(tmp_path, restore_engine):
    """Una DB sin sellar (pre-versionado) reporta 0, no rompe."""
    configure(str(tmp_path / "catalog_fresh.db"))
    assert get_schema_version() == 0


def test_string_default_with_quote_is_escaped(tmp_path, restore_engine):
    """F4: un default string con apóstrofe no debe romper el DDL del ALTER (el único
    propósito de la migración son columnas FUTURAS — un default tipo \"won't\" no
    puede tirar el arranque)."""
    from sqlalchemy import Column, MetaData, String, Table
    from core.infrastructure.db.catalog_repository import _migrate_table_add_columns

    eng = configure(str(tmp_path / "quote.db"))
    with eng.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t (id VARCHAR PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO t (id) VALUES ('x')")

    md = MetaData()
    table = Table("t", md,
                  Column("id", String, primary_key=True),
                  Column("note", String, nullable=False, default="won't apply"))

    _migrate_table_add_columns(eng, table)   # no debe levantar OperationalError

    with eng.begin() as conn:
        row = conn.exec_driver_sql("SELECT note FROM t WHERE id='x'").fetchone()
    assert row[0] == "won't apply", "el default con apóstrofe debe persistirse intacto"


def test_init_db_is_noop_after_first_call_same_engine(tmp_path, restore_engine, monkeypatch):
    """F5: init_db corre la migración UNA vez por engine; las ~39 llamadas posteriores
    (cada operación ABM) no deben re-inspeccionar ni re-escribir el stamp."""
    import core.infrastructure.db.catalog_repository as cr

    configure(str(tmp_path / "noop.db"))
    calls = {"n": 0}
    real = cr._migrate_table_add_columns
    monkeypatch.setattr(cr, "_migrate_table_add_columns",
                        lambda eng, t: (calls.__setitem__("n", calls["n"] + 1), real(eng, t))[1])

    init_db()
    first = calls["n"]
    assert first > 0, "la primera llamada migra"
    init_db()
    init_db()
    assert calls["n"] == first, "llamadas posteriores con el mismo engine deben ser no-op"


def test_init_db_reruns_after_engine_reconfigure(tmp_path, restore_engine, monkeypatch):
    """F5: reconfigurar el engine (otra DB — patrón de los tests) invalida el flag."""
    import core.infrastructure.db.catalog_repository as cr

    configure(str(tmp_path / "a.db"))
    calls = {"n": 0}
    real = cr._migrate_table_add_columns
    monkeypatch.setattr(cr, "_migrate_table_add_columns",
                        lambda eng, t: (calls.__setitem__("n", calls["n"] + 1), real(eng, t))[1])
    init_db()
    first = calls["n"]

    configure(str(tmp_path / "b.db"))   # engine nuevo → DB distinta
    init_db()
    assert calls["n"] > first, "un engine nuevo debe volver a migrar"


# ── v2: BOPREAL huérfanos retipados desde la ficha BYMA guardada ─────────────
def _db_v1_con(tmp_path, *filas):
    """DB con el schema completo, sellada en v1, con las filas dadas."""
    from datetime import date as _d

    from core.infrastructure.db.catalog_repository import Base, _stamp_schema_version
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    eng = configure(str(tmp_path / "v1.db"))
    Base.metadata.create_all(eng)
    with SessionLocal.begin() as s:
        for f in filas:
            fila = dict(day_count="ACT/365.25", cer_lag=10, payment_frequency=2)
            fila.update(f)
            s.add(InstrumentORM(**fila))
    _stamp_schema_version(eng, 1)
    assert get_schema_version() == 1
    return _d


_FICHA_BCRA = {"tipoEspecie": "Títulos Públicos", "securityType": "GO",
               "emisor": "Banco Central de la República Argentina",
               "ficha": {"moneda": "Dólares", "fecha_emision": "2025-06-24",
                         "fecha_vencimiento": "2028-10-31"}}


def _orm(ticker):
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM
    with SessionLocal() as s:
        o = s.get(InstrumentORM, ticker)
        assert o is not None, ticker
        return o


def test_v2_retipa_el_bopreal_huerfano_desde_la_ficha_byma(tmp_path, restore_engine):
    """BPOA8 (BOPREAL Serie 4A) quedó con `instrument_type='SOBERANOS'` —huérfano,
    invisible en todos los paneles— y sin vencimiento tras un round-trip del ABM, con
    la ficha BYMA intacta en `raw_fields["byma"]`. La migración v2 lo retipa desde esa
    ficha (emisor BCRA + hoja Soberanos = BOPREAL) y le repone vencimiento y emisión.
    Es la vía versionada de CLAUDE.md: corre sola al arrancar cada entorno, una vez."""
    d = _db_v1_con(tmp_path, dict(ticker="BPOA8", short_name="BPOA8", sheet="Soberanos",
                                  instrument_type="SOBERANOS", isin="AR0029227748",
                                  raw_fields={"origen": "IAMC", "byma": _FICHA_BCRA}))
    init_db()

    o = _orm("BPOA8")
    assert o.instrument_type == "BOPREAL"
    assert o.maturity_date == d(2028, 10, 31)
    assert o.emission_date == d(2025, 6, 24)
    assert o.day_count == "30/360"                     # convención BOPREAL (prospecto BCRA)
    # El blob del form también: sin `tipo` el próximo round-trip del ABM lo volvería a
    # perder. MERGE: `origen` y la ficha siguen ahí.
    assert o.raw_fields["tipo"] == "BOPREAL"
    assert o.raw_fields["fecha_vencimiento"] == "2028-10-31"
    assert o.raw_fields["origen"] == "IAMC" and o.raw_fields["byma"] == _FICHA_BCRA
    assert get_schema_version() == CURRENT_SCHEMA_VERSION >= 2


def test_v2_no_adivina_fuera_del_caso_bcra_ni_pisa_lo_que_ya_esta(tmp_path, restore_engine):
    """Sólo se retipa lo que la ficha permite afirmar: otro emisor queda huérfano (lo
    resuelve el operador por ABM), un BOPREAL ya bien tipado no se toca aunque tenga
    otro vencimiento cargado, y una fila sin ficha queda como está."""
    d = _db_v1_con(
        tmp_path,
        dict(ticker="XX1", short_name="x", sheet="Soberanos", instrument_type="SOBERANOS",
             raw_fields={"byma": {**_FICHA_BCRA, "emisor": "Gobierno Nacional"}}),
        dict(ticker="BPOB8", short_name="BPB8D", sheet="Soberanos", instrument_type="BOPREAL",
             maturity_date=__import__("datetime").date(2028, 10, 31), day_count="30/360",
             raw_fields={"tipo": "BOPREAL", "byma": _FICHA_BCRA}),
        dict(ticker="XX2", short_name="x", sheet="Soberanos", instrument_type="SOBERANOS",
             raw_fields={"origen": "IAMC"}),
        dict(ticker="XX3", short_name="x", sheet="Obligaciones_Negociables",
             instrument_type="OBLIGACIONES_NEGOCIABLES", raw_fields={"byma": _FICHA_BCRA}),
    )
    init_db()

    assert _orm("XX1").instrument_type == "SOBERANOS" and _orm("XX1").maturity_date is None
    assert _orm("BPOB8").instrument_type == "BOPREAL" and _orm("BPOB8").maturity_date == d(2028, 10, 31)
    assert _orm("XX2").instrument_type == "SOBERANOS"
    assert _orm("XX3").instrument_type == "OBLIGACIONES_NEGOCIABLES"   # otra hoja: no es BCRA-bono


# ── v3: deshacer la primera corrida del job de novedades ─────────────────────
def _db_v2_con_corrida(tmp_path):
    """DB sellada en v2 con el rastro de la corrida defectuosa: filas del seed vistas
    (AL30, del CSV), filas que sólo agregó el job (AA17 vencido; AO29X variante con el
    ISIN de un bono cargado), una fila ajena sin `last_seen`, novedades en los tres
    estados y las claves `universe_*`."""
    from core.infrastructure.db.catalog_repository import (
        Base, _ensure_schema_meta, _stamp_schema_version,
    )
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import BymaCatalogORM, UniverseNovedadORM

    eng = configure(str(tmp_path / "v2.db"))
    Base.metadata.create_all(eng)
    _ensure_schema_meta(eng)
    with SessionLocal.begin() as s:
        s.add_all([
            BymaCatalogORM(symbol="AL30", ticker_pesos="AL30", moneda="ARS", cotiza=1,
                           clase_liquidacion="primary", categoria="Títulos Públicos",
                           last_seen="2026-09-07"),
            BymaCatalogORM(symbol="AA17", ticker_pesos="AA17", moneda="ARS", cotiza=1,
                           clase_liquidacion="primary", categoria="Títulos Públicos",
                           last_seen="2026-09-07"),
            BymaCatalogORM(symbol="AO29X", ticker_pesos="AO29X", moneda="ARS", cotiza=1,
                           clase_liquidacion="primary", categoria="Títulos Públicos",
                           isin="AR0550263393", last_seen="2026-09-07"),
            BymaCatalogORM(symbol="ZZZ1", categoria="Obligaciones Negociables"),
        ])
        s.add_all([
            UniverseNovedadORM(symbol="AA17", first_seen="2026-09-07", source="data912",
                               categoria="Títulos Públicos", estado="nueva"),
            UniverseNovedadORM(symbol="AO29X", first_seen="2026-09-07", source="data912",
                               categoria="Títulos Públicos", estado="nueva"),
            UniverseNovedadORM(symbol="S29E7", first_seen="2026-09-07", source="byma",
                               categoria="Títulos Públicos", estado="cargada"),
            UniverseNovedadORM(symbol="GGAL2", first_seen="2026-09-07", source="byma",
                               categoria="Acciones", estado="descartada"),
        ])
        s.connection().exec_driver_sql(
            "INSERT INTO schema_meta (key, value) VALUES ('universe_ultima_corrida', '2026-09-07'), "
            "('universe_ultimos_vistos', '8747')")
    _stamp_schema_version(eng, 2)
    return eng


def _byma(symbol):
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import BymaCatalogORM
    with SessionLocal() as s:
        return s.get(BymaCatalogORM, symbol)


def test_v3_deshace_la_primera_corrida_del_universo(tmp_path, restore_engine):
    """La corrida del 2026-09-07 tomó como visto el esqueleto BYMA con precio 0 y las
    variantes como especies: +4410 filas en byma_catalog (17 con el ISIN de un bono
    cargado y cotiza=1 → el backfill de patas las habría escrito en instruments) y 4155
    novedades. v3 vuelve al seed, borra las `nueva` y las claves para que el job corra de
    nuevo con las reglas corregidas; lo decidido por el operador se conserva."""
    eng = _db_v2_con_corrida(tmp_path)
    init_db()

    assert _byma("AL30") is not None and _byma("AL30").last_seen is None   # del seed: queda
    assert _byma("AA17") is None and _byma("AO29X") is None                 # del job: fuera
    assert _byma("ZZZ1") is not None                                        # sin last_seen: ajena al job
    from core.infrastructure.byma import novedades as nov
    assert [f["symbol"] for f in nov.listar("nueva")] == []
    assert [f["symbol"] for f in nov.listar("cargada")] == ["S29E7"]
    assert [f["symbol"] for f in nov.listar("descartada")] == ["GGAL2"]
    assert nov.leer_meta("universe_ultima_corrida") is None
    assert nov.leer_meta("universe_ultimos_vistos") is None
    assert get_schema_version() == CURRENT_SCHEMA_VERSION >= 3
    with eng.begin() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM instruments").scalar() == 0


def test_v3_sin_csv_seed_solo_borra_las_variantes(tmp_path, restore_engine, monkeypatch):
    import core.infrastructure.db.catalog_repository as cr

    _db_v2_con_corrida(tmp_path)
    monkeypatch.setattr(cr, "_simbolos_del_seed", lambda: set())
    init_db()
    assert _byma("AA17") is not None and _byma("AA17").last_seen is None   # no se distingue: queda
    assert _byma("AO29X") is None                                          # dañina: fuera


def test_v3_es_no_op_donde_el_job_nunca_corrio(tmp_path, restore_engine, monkeypatch):
    import core.infrastructure.db.catalog_repository as cr

    _db_v1_con(tmp_path)
    leidos = {"n": 0}
    monkeypatch.setattr(cr, "_simbolos_del_seed",
                        lambda: (leidos.__setitem__("n", leidos["n"] + 1), set())[1])
    init_db()
    assert leidos["n"] == 0            # sin rastro del job no se lee el CSV siquiera
    assert get_schema_version() == CURRENT_SCHEMA_VERSION


def test_v2_no_vuelve_a_correr_sobre_una_db_ya_sellada(tmp_path, restore_engine, monkeypatch):
    """Exactamente una vez: con la DB en la versión vigente el paso v2 no se invoca."""
    import core.infrastructure.db.catalog_repository as cr

    _db_v1_con(tmp_path)
    init_db()
    assert get_schema_version() == CURRENT_SCHEMA_VERSION

    calls = {"n": 0}
    monkeypatch.setattr(cr, "_migrate_v2_bopreal_huerfanos",
                        lambda eng: (calls.__setitem__("n", calls["n"] + 1), 0)[1])
    configure(str(tmp_path / "v1.db"))          # engine nuevo sobre la MISMA DB ya sellada
    init_db()
    assert calls["n"] == 0


def test_concurrent_duplicate_column_is_tolerated(tmp_path, restore_engine, monkeypatch):
    """F6: si otro proceso agregó la columna entre el inspect y el ALTER (carrera de
    dos procesos sobre una DB con schema viejo), el 'duplicate column' no debe
    propagar — la columna ya existe, que era el objetivo."""
    from sqlalchemy import Column, MetaData, String, Table
    from core.infrastructure.db.catalog_repository import _migrate_table_add_columns

    eng = configure(str(tmp_path / "race.db"))
    with eng.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t (id VARCHAR PRIMARY KEY)")

    md = MetaData()
    table = Table("t", md,
                  Column("id", String, primary_key=True),
                  Column("extra", String, default=None))

    # La columna YA existe en la DB (el "otro proceso" ganó la carrera)...
    with eng.begin() as conn:
        conn.exec_driver_sql('ALTER TABLE t ADD COLUMN "extra" VARCHAR')

    # ...pero el inspector de ESTE proceso tiene una vista stale (no la ve).
    import core.infrastructure.db.catalog_repository as cr
    real_inspect = cr.inspect

    class _StaleInspector:
        def has_table(self, name):
            return True
        def get_columns(self, name):
            return [{"name": "id"}]   # vista vieja: sin 'extra'

    monkeypatch.setattr(cr, "inspect", lambda e: _StaleInspector())
    _migrate_table_add_columns(eng, table)   # ALTER duplicado → debe tolerarse

    cols = {c["name"] for c in real_inspect(eng).get_columns("t")}
    assert "extra" in cols
