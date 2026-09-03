"""Re-auditoría R5 — `deploy.sh` no puede crear un venv que la app no pueda usar.

Hallazgo de la re-auditoría del lote F_ops: el bootstrap del venv se hacía con
`python3 -m venv venv`, sin pinear la minor, mientras `run.py:13` aborta con
`SystemExit` en cualquier cosa que no sea 3.12 ("Requiere Python 3.12.x"). En un
droplet donde `python3` sea 3.10 o 3.13 el deploy instala TODAS las dependencias, hace
`systemctl restart` —bajando el servicio viejo, que sí andaba— y el fallo aparece
recién en el healthcheck, con el motivo real escondido en `journalctl`.

Los tests de acá EJECUTAN el bloque de preparación del venv de `deploy.sh` en un
sandbox con un PATH falso, así que fallan de verdad si alguien vuelve a `python3` pelado
(no es un test de texto: se comprueba el comportamiento).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")

# `noauth`: estos tests no tocan la app FastAPI — sin el marker, la fixture autouse
# `_auth_bypass` importaría apps.web.app sólo para saltearlo.
pytestmark = [
    pytest.mark.noauth,
    pytest.mark.skipif(BASH is None, reason="requiere bash (Git Bash / POSIX)"),
]

_INICIO = 'echo ">>> Preparando el entorno virtual..."'
_FIN = "# shellcheck source=/dev/null"


def _bloque_venv() -> str:
    """El fragmento de deploy.sh que resuelve/valida el intérprete del venv."""
    txt = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    i, j = txt.index(_INICIO), txt.index(_FIN)
    assert i < j, "deploy.sh reordenó la preparación del venv respecto del activate"
    return "set -euo pipefail\n" + txt[i:j]


def _fake_python(path: Path, version: str, crea_venv: bool = False) -> None:
    """Intérprete falso: responde `-V`, el probe `-c 'import sys; ...'` según su minor
    y —si corresponde— `-m venv <dir>` dejando un `bin/python` de la misma versión."""
    minor = version.split(".")[1]
    ok = "0" if minor == "12" else "1"
    body = f"""#!/bin/sh
case "$1" in
  -V|--version) echo "Python {version}"; exit 0 ;;
  -c) exit {ok} ;;
"""
    if crea_venv:
        body += """  -m) mkdir -p "$3/bin"
      cp "$0" "$3/bin/python"
      chmod +x "$3/bin/python"
      exit 0 ;;
"""
    body += """  *) exit 1 ;;
esac
"""
    path.write_text(body, encoding="utf-8", newline="\n")
    os.chmod(path, 0o755)


def _correr(sandbox: Path, binarios: Path) -> subprocess.CompletedProcess[str]:
    """Corre el bloque con `binarios` AL FRENTE del PATH: los intérpretes falsos
    tapan a cualquier python3/python3.12 real de la máquina. Detrás va sólo el
    directorio de utilidades del propio bash (mkdir/cp/chmod), que el venv falso usa."""
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(binarios), os.path.dirname(BASH)])
    return subprocess.run([BASH, "-c", _bloque_venv()], cwd=sandbox, env=env,
                          capture_output=True, text=True, timeout=120)


@pytest.fixture
def sandbox(tmp_path):
    (tmp_path / "bin").mkdir()
    return tmp_path


def test_aborta_si_no_hay_python312_en_el_droplet(sandbox):
    """Sin 3.12 disponible NO se crea ningún venv: se aborta ANTES del restart.

    Con `python3 -m venv venv` pelado este caso terminaba con un venv 3.13 creado y
    exit 0 — el deploy seguía hasta reiniciar el servicio y recién ahí moría."""
    # Los dos nombres que busca el script, ninguno en 3.12 (y tapan a los reales).
    _fake_python(sandbox / "bin" / "python3", "3.13.2", crea_venv=True)
    _fake_python(sandbox / "bin" / "python3.12", "3.13.2", crea_venv=True)

    proc = _correr(sandbox, sandbox / "bin")

    salida = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "deploy.sh creó el venv con un intérprete que NO es 3.12: run.py aborta con "
        f"SystemExit y el fallo aparece recién en el healthcheck.\n{salida}")
    assert not (sandbox / "venv").exists(), "quedó un venv inservible a medio crear"
    assert "3.12" in salida, f"el error no dice qué falta:\n{salida}"


def test_usa_python312_aunque_python3_sea_otra_minor(sandbox):
    """Con `python3.12` presente se lo prefiere, sin importar a qué apunte `python3`."""
    _fake_python(sandbox / "bin" / "python3", "3.13.2", crea_venv=True)
    _fake_python(sandbox / "bin" / "python3.12", "3.12.8", crea_venv=True)

    proc = _correr(sandbox, sandbox / "bin")

    salida = proc.stdout + proc.stderr
    assert proc.returncode == 0, salida
    creado = sandbox / "venv" / "bin" / "python"
    assert creado.is_file(), f"no creó el venv:\n{salida}"
    assert "3.12" in subprocess.run([BASH, str(creado), "-V"], capture_output=True,
                                    text=True).stdout, "el venv no quedó en 3.12"


def test_acepta_python3_cuando_ya_es_312(sandbox):
    """Un droplet donde `python3` YA es 3.12 (no hay binario `python3.12`) sigue
    andando: la validación es por versión, no por nombre del ejecutable."""
    _fake_python(sandbox / "bin" / "python3", "3.12.8", crea_venv=True)

    proc = _correr(sandbox, sandbox / "bin")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (sandbox / "venv" / "bin" / "python").is_file()


def test_no_toca_un_venv_312_ya_existente(sandbox):
    """Idempotencia: si el venv está y es 3.12, no se re-crea (ni se pide 3.12 al PATH)."""
    (sandbox / "venv" / "bin").mkdir(parents=True)
    _fake_python(sandbox / "venv" / "bin" / "python", "3.12.8")
    marca = sandbox / "venv" / "marca.txt"
    marca.write_text("no me toques", encoding="utf-8")

    proc = _correr(sandbox, sandbox / "bin")      # PATH vacío de intérpretes

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert marca.exists(), "re-creó el venv existente y se llevó su contenido"


def test_rechaza_un_venv_existente_que_no_es_312(sandbox):
    """Un venv heredado de otra minor tampoco puede pasar en silencio: el deploy
    aborta con instrucciones, en vez de reiniciar el servicio hacia un SystemExit."""
    (sandbox / "venv" / "bin").mkdir(parents=True)
    _fake_python(sandbox / "venv" / "bin" / "python", "3.10.14")

    proc = _correr(sandbox, sandbox / "bin")

    salida = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        f"activó un venv que no es 3.12: run.py aborta al arrancar.\n{salida}")
    assert "rm -rf venv" in salida, f"el error no dice cómo salir del paso:\n{salida}"
