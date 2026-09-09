"""Contrato del layout: una colisión no desplaza vecinos ni corrompe el estado.

Ejecuta el motor GridStack vendorizado real (sin browser ni paquetes npm nuevos).
La interacción y las medidas CSS se verifican aparte con Playwright y datos sintéticos.
"""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_dashboard_collision_and_saved_state_contract():
    node = shutil.which("node")
    if not node:
        pytest.skip("requiere node para probar el layout del dashboard")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "tests/js/dashboard_grid.test.cjs"],
        cwd=root, text=True, encoding="utf-8", capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
