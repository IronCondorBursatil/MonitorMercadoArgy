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
