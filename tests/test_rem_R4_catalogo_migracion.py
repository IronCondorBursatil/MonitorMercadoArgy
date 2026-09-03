"""Remediación lote R4_catalogo — contrato operativo de `scripts/migrate_orphan_types.py`.

El script arregla las 44 filas ya corrompidas de la `catalog.db` viva (43 ONs +
BPOA8, 127 especies). Es el único de mis items que ESCRIBE la base del usuario, así
que las cuatro garantías que lo hacen seguro tienen que estar atadas por test y no
por lectura:

  · **--dry-run por defecto**: sin `--apply` no se toca la DB ni se pide backup.
  · **backup ANTES de escribir**: `guard_write(tag='pre-orphan-types')` →
    `backup_db(..., tag=...)`; si el snapshot falla, se aborta (la escritura
    quedaría sin red). Se verifica el ORDEN, no solo que se llame.
  · **forward-only**: solo `UPDATE` de columnas existentes. Nunca DROP/DELETE, no
    pierde cashflows ni filas, y no pisa un dato existente con vacío.
  · **idempotente**: la segunda corrida no cambia nada (ni pide backup).

Los tests corren contra la DB temporal de `tmp_db`; NUNCA contra la del usuario.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import migrate_orphan_types as mig  # noqa: E402
import op_guards  # noqa: E402

_ON_SHEET = "Obligaciones_Negociables"


@pytest.fixture
def db(tmp_db):
    """Catálogo de prueba: 1 ON huérfana (con cashflow) + 1 fila sana."""
    from core.domain.models import Cashflow
    from core.infrastructure.db.catalog_repository import init_db, instrument_to_orm
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import CashflowORM, InstrumentORM

    init_db()
    with SessionLocal.begin() as s:
        s.add(InstrumentORM(
            ticker="BF39O", ticker_mep="BF39D", short_name="BBVA",
            instrument_type="OBLIGACIONES_NEGOCIABLES", sheet=_ON_SHEET,
            day_count="ACT/365.25", raw_fields={"origen": "IAMC", "ley_aplicable": "Argentina"},
            cashflows=[CashflowORM(ticker="BF39O", fecha_pago=date(2026, 12, 5),
                                   amortizacion=100.0, cupon_interes=2.9)],
        ))
        s.add(InstrumentORM(ticker="AL30", ticker_mep="AL30D", short_name="BONAR 2030",
                            instrument_type="BONAR", sheet="Soberanos",
                            maturity_date=date(2030, 7, 9), day_count="ACT/365.25",
                            raw_fields={"tipo": "BONAR"}))
    assert instrument_to_orm is not None      # import usado por el módulo bajo test
    assert Cashflow is not None
    return tmp_db


def _orm(ticker):
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM
    with SessionLocal() as s:
        o = s.get(InstrumentORM, ticker)
        return None if o is None else {
            "instrument_type": o.instrument_type, "maturity_date": o.maturity_date,
            "emission_date": o.emission_date, "day_count": o.day_count,
            "category": o.category, "raw_fields": dict(o.raw_fields or {}),
            "short_name": o.short_name, "n_cf": len(o.cashflows),
        }


@pytest.fixture
def guard_env(db, monkeypatch):
    """Apunta el preflight de `op_guards` a la DB del test.

    `guard_write` razona sobre `settings.catalog_db` mientras la migración escribe
    por el ENGINE. En producción son el MISMO archivo; bajo `tmp_db` no, y sin esto
    la rama 'bootstrap: no hay estado previo que respaldar' se dispara o no según
    qué otro test haya creado antes la DB del sandbox (test dependiente del orden)."""
    from types import SimpleNamespace

    monkeypatch.setattr(op_guards, "settings", SimpleNamespace(
        host="127.0.0.1", port=8000, catalog_db=str(db / "test.db"),
        backup_dir=str(db / "backups"), backup_keep=7))
    monkeypatch.setattr(op_guards, "server_running", lambda *a, **k: False)


@pytest.fixture
def backup_spy(guard_env, monkeypatch):
    """Intercepta el backup real y anota el ESTADO DE LA DB en el momento en que se
    toma (para probar que el snapshot precede a la escritura)."""
    calls = []

    def fake_backup(src, dest, keep=7, tag=None):
        calls.append({"tag": tag, "estado": _orm("BF39O")})
        return Path(dest) / f"catalog-{tag}.db"

    monkeypatch.setattr(op_guards, "backup_db", fake_backup)
    return calls


# --------------------------------------------------------------------------- #
# --dry-run por defecto
# --------------------------------------------------------------------------- #

def test_sin_apply_no_escribe_ni_pide_backup(db, backup_spy, capsys):
    antes = _orm("BF39O")
    assert mig.main([]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert _orm("BF39O") == antes
    assert backup_spy == []          # un dry-run no rota los backups del usuario


# --------------------------------------------------------------------------- #
# Backup ANTES de escribir
# --------------------------------------------------------------------------- #

def test_apply_toma_el_snapshot_tagged_antes_de_tocar_la_db(db, backup_spy):
    assert mig.main(["--apply"]) == 0
    assert len(backup_spy) == 1
    assert backup_spy[0]["tag"] == "pre-orphan-types"
    # el estado capturado durante el backup es el PREVIO: el snapshot precede al UPDATE
    assert backup_spy[0]["estado"]["instrument_type"] == "OBLIGACIONES_NEGOCIABLES"
    assert _orm("BF39O")["instrument_type"] == "HARD DOLLAR"


def test_apply_aborta_si_el_backup_falla(guard_env, monkeypatch, capsys):
    """`backup_db` devuelve None si la copia falló → no se escribe (rc 3)."""
    monkeypatch.setattr(op_guards, "backup_db", lambda *a, **k: None)
    antes = _orm("BF39O")
    assert mig.main(["--apply"]) == 3
    assert _orm("BF39O") == antes
    assert "sin red" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Idempotencia
# --------------------------------------------------------------------------- #

def test_la_segunda_corrida_no_cambia_nada_ni_pide_backup(db, backup_spy):
    assert mig.main(["--apply"]) == 0
    despues = _orm("BF39O")
    assert mig.main(["--apply"]) == 0
    assert _orm("BF39O") == despues
    assert len(backup_spy) == 1      # sin plan no hay preflight: no rota backups


# --------------------------------------------------------------------------- #
# Forward-only
# --------------------------------------------------------------------------- #

def test_no_borra_filas_ni_cashflows_ni_toca_las_sanas(db, backup_spy):
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import CashflowORM, InstrumentORM
    from sqlalchemy import func, select

    sana_antes = _orm("AL30")
    with SessionLocal() as s:
        n_inst = s.execute(select(func.count()).select_from(InstrumentORM)).scalar()
        n_cf = s.execute(select(func.count()).select_from(CashflowORM)).scalar()

    assert mig.main(["--apply"]) == 0

    with SessionLocal() as s:
        assert s.execute(select(func.count()).select_from(InstrumentORM)).scalar() == n_inst
        assert s.execute(select(func.count()).select_from(CashflowORM)).scalar() == n_cf
    assert _orm("AL30") == sana_antes           # la fila sana no entra al plan
    assert _orm("BF39O")["n_cf"] == 1           # el schedule sobrevive
    assert _orm("BF39O")["short_name"] == "BBVA"


def test_el_script_no_contiene_ningun_camino_destructivo():
    """Guard de código: la migración es solo UPDATE. Un DROP/DELETE acá borraría
    altas que viven SOLO en la DB (invariante forward-only de CLAUDE.md)."""
    src = Path(mig.__file__).read_text(encoding="utf-8").upper()
    for token in ("DROP TABLE", "DELETE FROM", "S.DELETE(", "DROP_ALL", "TRUNCATE"):
        assert token not in src, token


def test_no_pisa_un_dato_existente_con_vacio(db, backup_spy, monkeypatch):
    """Si la semilla no conoce un campo (None), el valor de la DB se conserva."""
    monkeypatch.setattr(mig, "target_fields", lambda t: {
        "instrument_type": "HARD DOLLAR", "maturity_date": None,
        "emission_date": None, "day_count": None, "category": None})
    with_dates = _orm("BF39O")
    assert mig.main(["--apply"]) == 0
    despues = _orm("BF39O")
    assert despues["instrument_type"] == "HARD DOLLAR"
    assert despues["day_count"] == with_dates["day_count"] == "ACT/365.25"
    assert despues["maturity_date"] is None


def test_el_merge_de_raw_fields_conserva_las_claves_previas(db, backup_spy):
    assert mig.main(["--apply"]) == 0
    raw = _orm("BF39O")["raw_fields"]
    assert raw["tipo"] == "HARD DOLLAR"          # el round-trip del ABM ya no lo pierde
    assert raw["origen"] == "IAMC"
    assert raw["ley_aplicable"] == "Argentina"


def test_la_escritura_es_atomica(db, monkeypatch):
    """Un fallo a mitad de camino no puede dejar el catálogo migrado a medias."""
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    with SessionLocal.begin() as s:
        s.add(InstrumentORM(ticker="BPOA8", short_name="BOPREAL 4A",
                            instrument_type="SOBERANOS", sheet="Soberanos",
                            day_count="ACT/365.25"))
    antes = {t: _orm(t) for t in ("BF39O", "BPOA8")}

    # `flag_modified` se importa DENTRO de apply_migration, así que el punto de
    # inyección es el módulo de SQLAlchemy, no el del script.
    import sqlalchemy.orm.attributes as attrs
    n = {"i": 0}
    original = attrs.flag_modified

    def boom(obj, key):
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("fallo simulado a mitad de la migración")
        return original(obj, key)

    monkeypatch.setattr(attrs, "flag_modified", boom)
    with pytest.raises(RuntimeError):
        mig.apply_migration()
    assert {t: _orm(t) for t in ("BF39O", "BPOA8")} == antes


def test_stamp_de_auditoria_en_schema_meta(db, backup_spy):
    """Queda registrado QUÉ migración corrió y cuándo (no gatea la idempotencia)."""
    from sqlalchemy import text

    from core.infrastructure.db.engine import SessionLocal

    assert mig.main(["--apply"]) == 0
    with SessionLocal() as s:
        v = s.execute(text("SELECT value FROM schema_meta WHERE key = :k"),
                      {"k": f"migration:{mig.MIGRATION_ID}"}).scalar()
    assert v and v.startswith("20")
