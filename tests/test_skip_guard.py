"""Guard de skips (Fase 3 de agents.md §0.8): un skip silencioso en el CI es invisible.

El runner ARM reportaba `2445 passed / 32 skipped` contra `2622 / 3` en local y nadie lo
denunciaba: 29 de esos skips eran los tests node de `fci.js` — la ÚNICA cobertura de
comportamiento del JS no corría en la pata de paridad con producción.

`tests/_skip_guard.py::skips_prohibidos(reasons, platform)` clasifica los motivos de skip de
una corrida y devuelve los que NO se toleran en esa plataforma:
  · `node` ausente: prohibido en TODAS (node es requisito del gate; el CI lo instala);
  · `requiere bash`: prohibido en Linux (ahí bash siempre está); tolerado en Windows;
  · `time.tzset() es sólo Unix`: tolerado en Windows, prohibido en Linux (ahí corre);
  · cualquier otro motivo: tolerado pero se lista (informativo) — la cuenta de skips
    depende del entorno (agents.md §0.6), así que no se asierta un número.
`tests/conftest.py` lo cablea con un hookwrapper de `pytest_runtestloop` que suma un fallo
a la sesión si hay prohibidos (exit 1, con el listado en el resumen).
"""

from __future__ import annotations

from tests._skip_guard import skips_prohibidos

NODE = "node no está en el PATH: el gate de Windows lo tiene; el droplet no corre pytest"
BASH = "requiere bash"
TZSET = "time.tzset() es sólo Unix"
OTRO = "fpdf2 no instalado"


def test_node_ausente_es_prohibido_en_todas_las_plataformas():
    assert skips_prohibidos([NODE], "win32") == [NODE]
    assert skips_prohibidos([NODE], "linux") == [NODE]
    assert skips_prohibidos(["Node.js no encontrado (shutil.which('node') is None)"], "linux")


def test_bash_y_tzset_dependen_de_la_plataforma():
    assert skips_prohibidos([BASH, TZSET], "win32") == []
    assert set(skips_prohibidos([BASH, TZSET], "linux")) == {BASH, TZSET}


def test_el_skip_especifico_de_windows_se_tolera_en_linux():
    # tests/test_timezone.py tiene DOS marcas: `_solo_unix` (reason "time.tzset() es sólo
    # Unix", que en Linux NO debe aparecer) y `_solo_windows` (reason "específico de Windows
    # (sin tzset)", que en Linux es un skip LEGÍTIMO). La primera versión del guard matcheaba
    # cualquier "tzset" y puso rojo el CI de la Fase 3 (run 34138568237) por el segundo.
    windows_only = "específico de Windows (sin tzset)"
    assert skips_prohibidos([windows_only], "linux") == []
    assert skips_prohibidos([windows_only], "win32") == []
    assert skips_prohibidos([TZSET, windows_only], "linux") == [TZSET]


def test_otros_motivos_se_toleran():
    assert skips_prohibidos([OTRO], "win32") == []
    assert skips_prohibidos([OTRO], "linux") == []


def test_lista_vacia_y_duplicados():
    assert skips_prohibidos([], "linux") == []
    assert skips_prohibidos([NODE, NODE, BASH], "linux") == [NODE, NODE, BASH]


def test_prefijo_skipped_de_pytest_se_tolera_en_el_motivo():
    # pytest antepone "Skipped: " al reason en algunos reports; no debe romper la clasificación
    assert skips_prohibidos(["Skipped: " + NODE], "linux") == ["Skipped: " + NODE]
