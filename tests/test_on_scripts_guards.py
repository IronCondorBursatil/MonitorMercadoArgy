"""Guards de los scripts de mantenimiento de ON (backfill/normalize/clases).

Los cuatro scripts hacen renames y escrituras MASIVAS sobre la catalog.db (fuente de
verdad, con altas que viven solo ahi). Llamaban a `backup_db(...)` y TIRABAN el
retorno: `backup_db` devuelve None cuando el snapshot falla, asi que un rename de 170
filas podia aplicarse sin respaldo recuperable. Tampoco miraban si el monitor estaba
vivo (el repo en memoria seguiria sirviendo el catalogo viejo hasta reiniciar).

`op_guards.guard_write` centraliza el patron que ya usaba ingest_master.py — este
archivo lo fija, mas el cableado en cada script.
"""

import pytest

from scripts import op_guards
from tests.conftest import listening_socket


@pytest.fixture
def guard_env(monkeypatch, tmp_path):
    """settings apuntando a una DB existente y a un puerto libre (server apagado)."""
    db = tmp_path / "catalog.db"
    db.write_bytes(b"x")
    s = listening_socket()
    host, port = s.getsockname()
    s.close()
    monkeypatch.setattr(op_guards.settings, "host", host)
    monkeypatch.setattr(op_guards.settings, "port", port)
    monkeypatch.setattr(op_guards.settings, "catalog_db", db)
    monkeypatch.setattr(op_guards.settings, "backup_dir", tmp_path / "backups")
    return tmp_path


def test_aborta_con_el_server_vivo(monkeypatch, guard_env, capsys):
    s = listening_socket()
    host, port = s.getsockname()
    monkeypatch.setattr(op_guards.settings, "host", host)
    monkeypatch.setattr(op_guards.settings, "port", port)
    llamado = {"backup": False}
    monkeypatch.setattr(op_guards, "backup_db",
                        lambda *a, **k: llamado.__setitem__("backup", True))
    try:
        assert op_guards.guard_write("pre-test") == 2
    finally:
        s.close()
    assert "ABORTADO" in capsys.readouterr().out
    assert llamado["backup"] is False   # ni siquiera llega a snapshotear


def test_con_server_vivo_force_sigue(monkeypatch, guard_env, tmp_path):
    s = listening_socket()
    host, port = s.getsockname()
    monkeypatch.setattr(op_guards.settings, "host", host)
    monkeypatch.setattr(op_guards.settings, "port", port)
    monkeypatch.setattr(op_guards, "backup_db", lambda *a, **k: tmp_path / "b.db")
    try:
        assert op_guards.guard_write("pre-test", force=True) == 0
    finally:
        s.close()


def test_aborta_si_el_snapshot_fallo_y_hay_db(monkeypatch, guard_env, capsys):
    """Es EL hallazgo: antes se descartaba el None y se escribia igual."""
    monkeypatch.setattr(op_guards, "backup_db", lambda *a, **k: None)
    assert op_guards.guard_write("pre-test") == 3
    assert "backup" in capsys.readouterr().out.lower()


def test_force_permite_seguir_sin_backup(monkeypatch, guard_env):
    monkeypatch.setattr(op_guards, "backup_db", lambda *a, **k: None)
    assert op_guards.guard_write("pre-test", force=True) == 0


def test_sin_db_previa_no_hay_nada_que_perder(monkeypatch, guard_env, tmp_path):
    """Bootstrap: backup None con DB inexistente NO aborta (mismo criterio que
    ingest_master)."""
    monkeypatch.setattr(op_guards.settings, "catalog_db", tmp_path / "no-existe.db")
    monkeypatch.setattr(op_guards, "backup_db", lambda *a, **k: None)
    assert op_guards.guard_write("pre-test") == 0


def test_snapshot_ok_usa_el_tag_pedido(monkeypatch, guard_env, tmp_path):
    visto = {}

    def _fake(db_path, backup_dir, **kw):
        visto.update(kw)
        return tmp_path / "catalog-2026-01-02T100000-pre-test.db"

    monkeypatch.setattr(op_guards, "backup_db", _fake)
    assert op_guards.guard_write("pre-test") == 0
    assert visto["tag"] == "pre-test"   # INCONDICIONAL, no el diario del arranque


# --------------------------------------------------------------------------- #
# Cableado: todo script que escriba tiene que pasar por el guard antes de commitear.
# Son tres y no cuatro porque `ingest_ypf_clases` quedo como shim de
# `ingest_on_clases` (C7) y hereda el guard del motor — lo fija test_ingest_clases.
# --------------------------------------------------------------------------- #
SCRIPTS = [
    "scripts.backfill_on_emisor",
    "scripts.normalize_on_emisor",
    "scripts.ingest_on_clases",
]


@pytest.mark.parametrize("modname", SCRIPTS)
def test_el_script_aborta_si_el_guard_dice_que_no(monkeypatch, modname):
    """Si el guard falla, el script sale con SU codigo y NO llega a commitear."""
    import importlib

    mod = importlib.import_module(modname)
    monkeypatch.setattr(mod, "guard_write", lambda tag, force=False: 3)

    class _SesionQueExplota:
        def __enter__(self):
            raise AssertionError("no deberia abrir sesion: el guard aborto antes")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "SessionLocal", lambda: _SesionQueExplota())
    assert mod.main(dry=False, **_extra_kwargs(mod)) == 3


def _extra_kwargs(mod):
    """ingest_on_clases toma ademas la ruta del CSV."""
    import inspect

    params = inspect.signature(mod.main).parameters
    return {"ruta": mod.DEFAULT} if "ruta" in params else {}


def test_ingest_iamc_aborta_si_el_guard_dice_que_no(monkeypatch):
    """`ingest_iamc_2026_08` no entra en la lista parametrizada porque su `main`
    toma `dry_run=` (no `dry=`) y abre la sesion con `SessionLocal.begin()`.

    Es el script que el pase de guards habia SALTEADO: tenia el backup inline con
    el retorno descartado (imprimia "backup pre-op: None" y daba de alta igual),
    sin `server_running` ni `--force`. El preflight va ANTES de validar los specs,
    asi que con el guard en rojo ni siquiera lee el JSON de la fuente.
    """
    import scripts.ingest_iamc_2026_08 as mod

    monkeypatch.setattr(mod, "guard_write", lambda tag, force=False: 3)
    monkeypatch.setattr(mod, "_load_specs",
                        lambda: pytest.fail("no deberia leer specs: el guard aborto antes"))
    assert mod.main(dry_run=False) == 3


def test_ingest_iamc_dry_run_no_pasa_por_el_guard(monkeypatch):
    """El dry-run se saltea el preflight a proposito (no escribe): es el uso normal
    para previsualizar con el monitor arriba."""
    import scripts.ingest_iamc_2026_08 as mod

    monkeypatch.setattr(mod, "guard_write",
                        lambda tag, force=False: pytest.fail("el dry-run no debe guardear"))
    assert mod.main(dry_run=True) == 0


def test_el_shim_ypf_hereda_el_guard(monkeypatch):
    """El shim no repite el preflight: si el motor aborta, el shim devuelve eso."""
    import scripts.ingest_on_clases as ic
    import scripts.ingest_ypf_clases as ypf

    monkeypatch.setattr(ic, "guard_write", lambda tag, force=False: 2)
    assert ypf.main(dry=False) == 2
