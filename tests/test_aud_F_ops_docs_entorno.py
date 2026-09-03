"""Auditoría F_ops — los comentarios del entorno no pueden contradecir los invariantes.

Dos afirmaciones falsas verificadas contra la máquina y el git config vivos:

  · `run.py` y `scripts/run_server_test.py` mandaban al "Microsoft Store" Python
    (`%LOCALAPPDATA%\\Microsoft\\WindowsApps\\python3.12.exe`), que YA NO EXISTE —
    el runtime es el de Programs (`py -3.12`), invariante de CLAUDE.md. En
    `run_server_test.py` no era un comentario pasivo sino un comando copiable que
    falla al ejecutarse.
  · `scripts/check.ps1` decía «No hay CI ni remoto (repo local) … mergear a master».
    Hay remoto (`origin` → github.com/IronCondorBursatil/MonitorMercadoArgy) y el
    trunk es `main` (`deploy.sh` hace `git pull origin main`). Quien lea eso mergea a
    `master`, la rama que el deploy NO usa, y el cambio nunca llega al droplet.

No es testeable "de verdad" (es prosa), pero sí es un guard de regresión barato: la
ruta muerta no puede volver a aparecer en un archivo ejecutable del repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INTERPRETE_MUERTO = "WindowsApps"


def _fuentes():
    yield ROOT / "run.py"
    yield ROOT / "run.bat"
    for p in sorted((ROOT / "scripts").glob("*.py")):
        yield p
    for p in sorted((ROOT / "scripts").glob("*.ps1")):
        yield p


@pytest.mark.parametrize("path", list(_fuentes()), ids=lambda p: p.name)
def test_no_manda_al_store_python_que_ya_no_existe(path):
    txt = path.read_text(encoding="utf-8", errors="replace")
    assert INTERPRETE_MUERTO not in txt, (
        f"{path.name} apunta al Store Python; el runtime es "
        r"%LOCALAPPDATA%\Programs\Python\Python312 (py -3.12)")


def test_run_py_no_dice_que_el_runtime_es_el_store_python():
    """El guard de versión de run.py explicaba el pin apuntando a un intérprete que
    no existe: quien se come el SystemExit sale a buscar el Store Python."""
    txt = (ROOT / "run.py").read_text(encoding="utf-8")
    assert "Microsoft Store" not in txt
    assert "py -3.12" in txt


def test_check_ps1_no_afirma_que_no_hay_remoto():
    txt = (ROOT / "scripts" / "check.ps1").read_text(encoding="utf-8")
    assert "repo local" not in txt
    assert "mergear a master" not in txt
    assert "main" in txt, "el gate tiene que nombrar el trunk real (origin/main)"
