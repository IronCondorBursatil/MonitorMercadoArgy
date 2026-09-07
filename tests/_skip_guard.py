"""Clasificación de motivos de skip: cuáles NO se toleran en cada plataforma.

Motivo (agents.md §0.6 y §0.8 Fase 3): el runner ARM del CI llegó a `2445 passed / 32
skipped` sin que nadie lo notara — 29 eran los tests node de `fci.js`, la única cobertura
de comportamiento del JS, salteados por falta de `node`. Un skip silencioso en el CI es
invisible; éste lo vuelve rojo.

No se asierta una CUENTA de skips (depende del entorno: bash/node en PATH, plataforma),
sino los MOTIVOS:
  · `node`: prohibido en todas las plataformas — node es requisito del gate (el CI lo
    instala con actions/setup-node; en la laptop está en C:\\Program Files\\nodejs);
  · `requiere bash`: prohibido en Linux (bash siempre está); tolerado en Windows, donde
    depende de que Git Bash esté en el PATH del proceso;
  · `time.tzset() es sólo Unix`: tolerado en Windows (no existe), prohibido en Linux (ahí
    esos tests tienen que correr);
  · cualquier otro motivo se tolera — se lista igual en el resumen para que se vea.

Lo cablea tests/conftest.py (hookwrapper de `pytest_runtestloop` + `pytest_terminal_summary`).
Pura y sin pytest a propósito: se testea en tests/test_skip_guard.py.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable

_NODE = re.compile(r"\bnode(?:\.js)?\b", re.I)
_BASH = re.compile(r"requiere bash", re.I)
_TZSET = re.compile(r"tzset", re.I)


def skips_prohibidos(reasons: Iterable[str], platform: str | None = None) -> list[str]:
    """Devuelve, en orden y con duplicados, los motivos de skip que no se toleran en
    `platform` (`sys.platform` por default: 'win32', 'linux', 'darwin'...)."""
    plat = platform or sys.platform
    es_windows = plat.startswith("win")
    malos: list[str] = []
    for reason in reasons:
        if _NODE.search(reason):
            malos.append(reason)
        elif not es_windows and (_BASH.search(reason) or _TZSET.search(reason)):
            malos.append(reason)
    return malos
