"""Auditoría F_ops — los 15 scripts de mantenimiento del catálogo que escribían sin red.

`backup_db` devuelve `None` cuando el snapshot falla. Estos scripts hacían
`snap = backup_db(...)`, imprimían el `None` y escribían igual sobre la `catalog.db`
(fuente de verdad: las altas del ABM viven SOLO ahí), sin mirar tampoco si el monitor
estaba vivo. `scripts/op_guards.guard_write` existe justo para eso y ya lo usan los 4
scripts migrados en a115f7e — la migración había quedado a medias.

El impacto no es uniforme:

  · GRAVE — `pin_irregular_to_explicit` y `pin_capital_factor_cashflows` mutan N bonos
    en loop y ante divergencia hacen `restore_db(bkp, ...)`. Con el snapshot fallado
    `bkp` es `None` y el ROLLBACK explota (`Path(None)` → TypeError): el catálogo queda
    mutado a medias, sin restore y sin backup.
  · SERIO — `ingest_on_iamc_2026_08` asigna `orm.raw_fields = {...}` entero sobre cada
    ticker existente, borrando `serie_clase` / `sector_override` / la ficha `byma`. Es
    el mismo bug que a115f7e arregló en su hermano `ingest_iamc_2026_08` (MERGE).
  · Higiene — el resto (altas puntuales por `save_instrument`, radio 1 bono).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from scripts import op_guards

ROOT = Path(__file__).resolve().parent.parent

# Los 15 que escriben sobre la catalog.db y descartaban el retorno del snapshot.
ESCRIBEN = [
    "backfill_on_ccy_legs",
    "fix_on_plc2_schedule",
    "ingest_irsa_ons",
    "ingest_on_iamc_2026_08",
    "ingest_on_lms8o",
    "ingest_on_mcc1o",
    "ingest_on_pecno",
    "ingest_on_vscpo",
    "ingest_on_xmc1o",
    "ingest_on_yfcio",
    "ingest_on_ypc4o",
    "ingest_ons_aec_aer_baca",
    "ingest_ons_arcor_raghsa_cgc",
    "pin_capital_factor_cashflows",
    "pin_irregular_to_explicit",
]


def _called_names(tree: ast.AST) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return out


@pytest.mark.parametrize("modname", ESCRIBEN)
def test_el_preflight_va_por_op_guards_y_no_por_backup_db_suelto(modname):
    """El snapshot pre-op tiene que salir de `op_guards`, que VERIFICA el retorno.

    Un `backup_db(...)` directo en el script es exactamente el patrón que dejaba
    escribir sin red (y, en los `pin_*`, el `None` que rompía el rollback).
    """
    src = (ROOT / "scripts" / f"{modname}.py").read_text(encoding="utf-8")
    llamadas = _called_names(ast.parse(src))

    assert "backup_db" not in llamadas, (
        f"{modname} sigue llamando backup_db directo (retorno sin verificar)")
    assert llamadas & {"guard_write", "guard_write_snapshot"}, (
        f"{modname} no pasa por op_guards.guard_write antes de escribir")
    assert re.search(r"from scripts\.op_guards import .*guard_write", src), (
        f"{modname} no importa el guard de op_guards")


@pytest.fixture
def snapshot_roto(monkeypatch, tmp_path):
    """Server apagado + `backup_db` fallando con una catalog.db existente: el guard
    tiene que abortar con 3 (es el escenario del disco lleno / WAL tomado)."""
    db = tmp_path / "catalog.db"
    db.write_bytes(b"x")
    monkeypatch.setattr(op_guards.settings, "catalog_db", db)
    monkeypatch.setattr(op_guards.settings, "backup_dir", tmp_path / "backups")
    monkeypatch.setattr(op_guards, "server_running", lambda *a, **k: False)
    monkeypatch.setattr(op_guards, "backup_db", lambda *a, **k: None)
    return monkeypatch


def test_pin_irregular_no_escribe_sin_snapshot(snapshot_roto):
    """Antes: escribía los N bonos y, si algo divergía, el restore reventaba con
    `TypeError: argument should be a str or an os.PathLike object ... not 'NoneType'`."""
    import scripts.pin_irregular_to_explicit as mod

    g = {"sheet": "Obligaciones_Negociables",
         "fields": {"ticker": "XXXO", "prox_cupon": "2026-01-01"},
         "cashflows": [{"date": "2026-01-01", "amortization": 100.0, "interest": 1.0}]}
    snapshot_roto.setattr(mod, "list_instruments", lambda: [{"key": "XXXO"}])
    snapshot_roto.setattr(mod, "get_instrument", lambda k: g)
    snapshot_roto.setattr(mod, "save_instrument",
                          lambda *a, **k: pytest.fail("escribió sin red de seguridad"))
    snapshot_roto.setattr(mod, "backup_db", lambda *a, **k: None, raising=False)

    assert mod.main() == 3


def test_pin_capital_factor_no_escribe_sin_snapshot(snapshot_roto):
    import scripts.pin_capital_factor_cashflows as mod

    g = {"sheet": "Obligaciones_Negociables",
         "fields": {"ticker": "XXXO", "capital factor": 0.5},
         "cashflows": [{"date": "2026-01-01", "amortization": 100.0, "interest": 1.0}]}
    snapshot_roto.setattr(mod, "list_instruments", lambda: [{"key": "XXXO"}])
    snapshot_roto.setattr(mod, "get_instrument", lambda k: g)
    snapshot_roto.setattr(mod, "save_instrument",
                          lambda *a, **k: pytest.fail("escribió sin red de seguridad"))
    snapshot_roto.setattr(mod, "backup_db", lambda *a, **k: None, raising=False)

    assert mod.main(dry=False) == 3


def test_alta_puntual_aborta_si_el_guard_dice_que_no(monkeypatch):
    """Representante de las 10 altas puntuales (`save_instrument`)."""
    import apps.web.instruments_abm as abm
    import scripts.ingest_on_lms8o as mod

    monkeypatch.setattr(mod, "guard_write", lambda tag, force=False: 3, raising=False)
    monkeypatch.setattr(abm, "save_instrument",
                        lambda *a, **k: pytest.fail("el guard abortó: no debe escribir"))

    assert mod.main(dry_run=False) == 3


def test_ingest_on_iamc_aborta_antes_de_tocar_la_sesion(monkeypatch):
    """El alta masiva reemplaza el cronograma completo de cada bono: el preflight va
    ANTES de todo, como en su hermano `ingest_iamc_2026_08`."""
    import core.infrastructure.db.engine as engine
    import scripts.ingest_on_iamc_2026_08 as mod

    monkeypatch.setattr(mod, "guard_write", lambda tag, force=False: 2, raising=False)
    monkeypatch.setattr(engine, "SessionLocal",
                        lambda *a, **k: pytest.fail("el guard abortó: no debe abrir sesión"))

    assert mod.main(dry_run=False) == 2


def test_ingest_on_iamc_dry_run_no_pasa_por_el_guard(monkeypatch):
    """El dry-run no escribe: es el uso normal con el monitor arriba."""
    import scripts.ingest_on_iamc_2026_08 as mod

    monkeypatch.setattr(mod, "guard_write",
                        lambda tag, force=False: pytest.fail("el dry-run no debe guardear"),
                        raising=False)

    assert mod.main(dry_run=True) == 0


# --------------------------------------------------------------------------- #
# raw_fields del alta masiva: MERGE, no reemplazo (mismo criterio que a115f7e
# aplicó en `ingest_iamc_2026_08`; acá seguía siendo una asignación entera).
# --------------------------------------------------------------------------- #
def test_iamc_on_preserva_las_claves_de_otros_productores():
    from scripts.ingest_on_iamc_2026_08 import merge_raw_fields

    previo = {"serie_clase": "Clase XXXIX (39)", "sector_override": "Energía",
              "byma": {"emisor": "ACME S.A."}}
    out = merge_raw_fields(previo, "Argentina", "7,5")

    assert out["serie_clase"] == "Clase XXXIX (39)"
    assert out["sector_override"] == "Energía"
    assert out["byma"] == {"emisor": "ACME S.A."}
    assert out["ley_aplicable"] == "Argentina"
    assert out["cupon_anual_pct"] == "7,5"
    assert "IAMC" in out["origen"]
    assert out is not previo and previo.get("origen") is None


def test_iamc_on_una_ley_ausente_no_pisa_la_que_ya_estaba():
    """El motor elige MEP (ley AR) vs CCL (Extranjera) con este campo."""
    from scripts.ingest_on_iamc_2026_08 import merge_raw_fields

    assert merge_raw_fields({"ley_aplicable": "Extranjera"}, None, "5")["ley_aplicable"] \
        == "Extranjera"
