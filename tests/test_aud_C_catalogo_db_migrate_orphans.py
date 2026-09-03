"""Auditoría lote C — migración de las 44 filas ya corrompidas en la DB viva.

Mitad (b) del hallazgo #6: el fix de código evita que ENTREN filas nuevas con un
tipo huérfano, pero las 43 ONs + BPOA8 que ya están en `catalog.db` siguen
invisibles. `scripts/migrate_orphan_types.py` las reclasifica desde la semilla
autoritativa del IAMC (idempotente, --dry-run por defecto, backup pre-op).
"""

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import migrate_orphan_types as mig  # noqa: E402


def _row(**kw):
    base = dict(ticker="X", ticker_mep=None, ticker_ccl=None, sheet="",
                instrument_type="", maturity_date=None, emission_date=None,
                day_count="ACT/365.25", category=None, raw_fields=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_plan_reclasifica_la_on_hard_dollar():
    rows = [_row(ticker="BF39O", ticker_mep="BF39D", sheet="Obligaciones_Negociables",
                 instrument_type="OBLIGACIONES_NEGOCIABLES")]
    plan = mig.build_plan(rows)
    assert len(plan) == 1
    ch = plan[0]["changes"]
    assert ch["instrument_type"] == ("OBLIGACIONES_NEGOCIABLES", "HARD DOLLAR")
    assert ch["maturity_date"][1] == date(2026, 12, 5)
    assert ch["emission_date"][1] == date(2025, 12, 5)
    assert ch["day_count"] == ("ACT/365.25", "ACT/365")   # convención ON (agents.md)
    assert ch["category"][1] == "Obligaciones Negociables"


def test_plan_reclasifica_la_on_dollar_linked():
    rows = [_row(ticker="CP39O", sheet="Obligaciones_Negociables",
                 instrument_type="OBLIGACIONES_NEGOCIABLES")]
    ch = mig.build_plan(rows)[0]["changes"]
    assert ch["instrument_type"][1] == "DOLLAR LINKED"


def test_plan_reclasifica_bpoa8_como_bopreal():
    rows = [_row(ticker="BPOA8", ticker_mep="BPA8D", ticker_ccl="BPA8C",
                 sheet="Soberanos", instrument_type="SOBERANOS")]
    ch = mig.build_plan(rows)[0]["changes"]
    assert ch["instrument_type"] == ("SOBERANOS", "BOPREAL")
    assert ch["maturity_date"][1] == date(2028, 10, 31)
    assert ch["day_count"] == ("ACT/365.25", "30/360")   # BOPREAL: prospecto BCRA


def test_plan_ignora_las_filas_sanas():
    rows = [_row(ticker="AL30", sheet="Soberanos", instrument_type="BONAR"),
            _row(ticker="GGAL", sheet="Acciones", instrument_type="ACCION")]
    assert mig.build_plan(rows) == []


def test_plan_reporta_los_huerfanos_sin_fuente():
    rows = [_row(ticker="NOEXISTE", sheet="Obligaciones_Negociables",
                 instrument_type="OBLIGACIONES_NEGOCIABLES")]
    plan = mig.build_plan(rows)
    assert plan == []
    assert mig.unresolved(rows) == ["NOEXISTE"]


def test_plan_es_idempotente(tmp_db):
    """Aplicar dos veces no vuelve a cambiar nada (el 2º plan es vacío)."""
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    init_db()
    with SessionLocal.begin() as s:
        s.add(InstrumentORM(ticker="BF39O", ticker_mep="BF39D", short_name="BBVA",
                            instrument_type="OBLIGACIONES_NEGOCIABLES",
                            sheet="Obligaciones_Negociables", day_count="ACT/365.25",
                            raw_fields={"origen": "IAMC"}))
        s.add(InstrumentORM(ticker="BPOA8", short_name="BOPREAL Serie 4A",
                            instrument_type="SOBERANOS", sheet="Soberanos",
                            day_count="ACT/365.25"))

    assert mig.apply_migration() == 2
    with SessionLocal() as s:
        assert s.get(InstrumentORM, "BF39O").instrument_type == "HARD DOLLAR"
        assert s.get(InstrumentORM, "BF39O").maturity_date == date(2026, 12, 5)
        # el `tipo` queda también en raw_fields → el form del ABM hace round-trip
        assert s.get(InstrumentORM, "BF39O").raw_fields["tipo"] == "HARD DOLLAR"
        assert s.get(InstrumentORM, "BF39O").raw_fields["origen"] == "IAMC"
        assert s.get(InstrumentORM, "BPOA8").instrument_type == "BOPREAL"

    assert mig.apply_migration() == 0   # idempotente


def test_migracion_deja_el_catalogo_sin_huerfanos(tmp_db):
    from apps.web.instruments_abm import audit_orphan_types
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    init_db()
    with SessionLocal.begin() as s:
        s.add(InstrumentORM(ticker="CACAO", short_name="X",
                            instrument_type="OBLIGACIONES_NEGOCIABLES",
                            sheet="Obligaciones_Negociables"))
    assert audit_orphan_types()
    mig.apply_migration()
    assert audit_orphan_types() == []


def test_main_es_dry_run_por_defecto(tmp_db, capsys):
    """Sin --apply NO escribe (y lo dice)."""
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    init_db()
    with SessionLocal.begin() as s:
        s.add(InstrumentORM(ticker="BF39O", short_name="BBVA",
                            instrument_type="OBLIGACIONES_NEGOCIABLES",
                            sheet="Obligaciones_Negociables"))

    assert mig.main([]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "BF39O" in out and "HARD DOLLAR" in out
    with SessionLocal() as s:
        assert s.get(InstrumentORM, "BF39O").instrument_type == "OBLIGACIONES_NEGOCIABLES"


def test_main_apply_aborta_si_el_server_esta_vivo(tmp_db, capsys, monkeypatch):
    """Hereda el preflight de op_guards: no se escribe con el monitor corriendo."""
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    init_db()
    with SessionLocal.begin() as s:
        s.add(InstrumentORM(ticker="BF39O", short_name="BBVA",
                            instrument_type="OBLIGACIONES_NEGOCIABLES",
                            sheet="Obligaciones_Negociables"))

    monkeypatch.setattr(mig, "guard_write", lambda tag, force=False: 2)
    assert mig.main(["--apply"]) == 2
    with SessionLocal() as s:
        assert s.get(InstrumentORM, "BF39O").instrument_type == "OBLIGACIONES_NEGOCIABLES"


@pytest.mark.parametrize("tipo,esperado", [("HD", "HARD DOLLAR"),
                                           ("DL", "DOLLAR LINKED"),
                                           ("ZC", "DOLLAR LINKED")])
def test_mapeo_tipo_iamc(tipo, esperado):
    assert mig._ON_TYPE[tipo] == esperado
