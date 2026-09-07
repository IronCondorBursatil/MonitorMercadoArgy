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
