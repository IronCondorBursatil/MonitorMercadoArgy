"""Auditoría G_tests — la red de equivalencia del motor no puede apagarse sola.

`tests/test_pricing_equivalence.py` importa el motor congelado dentro de un
try/except y marca los comparativos con `skipif(Old is None)`. Si el import de
`tests/_legacy_engine.py` se rompe (un renombre de los símbolos VIVOS que importa
—`_xirr_from_years`, `days_30_360`, `core.domain.pricing.metrics`— o CUALQUIER
excepción en import-time: el `except Exception` traga todo), los tres comparativos
se auto-skipean, `pytest` sale 0 y `scripts/check.ps1` da GATE VERDE con el
invariante #1 de CLAUDE.md sin haberse ejecutado.

Verificación de punta a punta: se corre la suite de equivalencia en un subproceso
con un `tests._legacy_engine` roto inyectado en `sys.modules` y se exige que el
exit code sea != 0 *por el guard* (no por daño colateral).
"""

import os
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUARD = "test_legacy_engine_importable"

# Plugin de pytest (se carga ANTES de la colección): un módulo `tests._legacy_engine`
# vacío en sys.modules hace que `from tests._legacy_engine import FinancialEngine`
# tire ImportError, exactamente como lo haría un renombre en core.domain.
_BROKEN_PLUGIN = """
import sys
import types

sys.modules["tests._legacy_engine"] = types.ModuleType("tests._legacy_engine")
"""


@pytest.mark.noauth
def test_equivalence_suite_falla_ruidosa_si_el_motor_legacy_no_importa(tmp_path):
    plugin_dir = tmp_path / "plug"
    plugin_dir.mkdir()
    (plugin_dir / "_broken_legacy_engine.py").write_text(_BROKEN_PLUGIN, encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(plugin_dir), _REPO_ROOT])
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("PYTEST_ADDOPTS", None)
    # Sandbox de bases propio: el subproceso re-importa tests/conftest.py, que purga su
    # dir de .db al arrancar. Con el dir FIJO compartido le borraría las bases a la
    # sesión PADRE, que las tiene abiertas (en Windows el lock del SO lo tapa; en POSIX
    # el unlink procede y la sesión se queda sin catalog.db a mitad de la corrida).
    sandbox = tmp_path / "dbs"
    sandbox.mkdir()
    env["MONITOR_TEST_DB_DIR"] = str(sandbox)

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_pricing_equivalence.py",
         "-q", "-p", "_broken_legacy_engine", "-p", "no:cacheprovider"],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    out = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
    assert proc.returncode != 0, (
        "La red de equivalencia del motor se auto-desactivó EN SILENCIO: con "
        "tests/_legacy_engine roto, pytest salió 0 y el gate queda verde.\n" + out
    )
    assert _GUARD in out, (
        f"La suite falló, pero NO por el guard `{_GUARD}` (¿daño colateral?).\n" + out
    )


def test_el_guard_del_motor_legacy_no_esta_skipeado():
    """El guard tiene que correr SIEMPRE: si alguien le cuelga un `skipif(Old is
    None)` (como los tres comparativos) vuelve el agujero silencioso."""
    import tests.test_pricing_equivalence as eq

    guard = getattr(eq, _GUARD, None)
    assert guard is not None, (
        "tests/test_pricing_equivalence.py perdió el guard de import del motor legacy")
    marks = [m.name for m in getattr(guard, "pytestmark", [])]
    assert "skipif" not in marks, f"el guard `{_GUARD}` no puede llevar skipif"


def test_el_guard_falla_cuando_el_motor_legacy_es_none(monkeypatch):
    """El guard falla (no skipea, no pasa) cuando el import quedó en None."""
    import tests.test_pricing_equivalence as eq

    monkeypatch.setattr(eq, "Old", None)
    monkeypatch.setattr(eq, "_LEGACY_IMPORT_ERROR", ImportError("boom"), raising=False)
    with pytest.raises(AssertionError):
        getattr(eq, _GUARD)()


# --------------------------------------------------------------------------- #
# Hallazgo 2: la equivalencia no comparaba NADA para DICP/DIP0/CUAP (los únicos
# instrumentos con capital_factor > 1): a los precios fijos 95/130/158.2 la TIR
# daba None en los DOS motores y `_close(None, None)` es True.
# --------------------------------------------------------------------------- #
_CAPITALIZING_CER = ("DICP", "DIP0", "CUAP")


def test_los_bonos_cer_capitalizados_se_comparan_con_tir_real(monkeypatch):
    from datetime import timedelta

    from config.settings import settings
    from core.domain.models import MarketSnapshot
    from core.domain.services import FinancialEngine as New
    from core.infrastructure.repositories import ExcelInstrumentsRepository
    from tests._clock import ref_date
    from tests.test_pricing_equivalence import MockFx, MockIndices, _test_prices

    monkeypatch.setenv("MONITOR_AS_OF", ref_date().isoformat())
    idx, fx = MockIndices(), MockFx()
    ref = ref_date()
    settle = ref + timedelta(days=1)

    insts = {i.ticker: i for i in
             ExcelInstrumentsRepository(str(settings.master_xlsx)).get_all_instruments()}
    for ticker in _CAPITALIZING_CER:
        inst = insts.get(ticker)
        if inst is None:          # el master es editable: no clavar el universo
            continue
        tirs = [New.calculate_tir(
                    MarketSnapshot(instrument=inst, price=p, last_update=ref),
                    idx, fx, settle_date=settle)
                for p in _test_prices(inst, idx, fx, ref)]
        assert any(t is not None and t == t for t in tirs), (
            f"{ticker}: los precios del harness de equivalencia dan TIR None en los "
            f"3 casos ({tirs!r}) → la comparación legacy-vs-nuevo es vacua")
