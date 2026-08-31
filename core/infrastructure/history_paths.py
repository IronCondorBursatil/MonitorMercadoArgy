"""Semilla versionada (repo) vs. estado de runtime (db_dir) para las series en disco.

POR QUE EXISTE ESTE MODULO
--------------------------
Varias series se persisten como archivos planos: CER/TAMAR/A3500/reservas
(`indices_provider`), el BEI diario (`apps/cli/bei.py`) y el corte CAFCI
(`cafci_provider`). Todos vivian en `data/history/`, que esta **versionado en git**,
y todos se reescriben en cada ciclo del refresh loop.

El efecto en el droplet era que el working tree quedaba modificado de forma
permanente, y por lo tanto **`git pull` abortaba en cada deploy**. Diagnosticado en
produccion el 2026-08-31: los seis CSV aparecian a la vez como borrados en el indice
y sin trackear en disco, y el arbol no se podia limpiar sin perder datos reales.

LA REGLA
--------
- `data/history/` = SEMILLA. Versionada, read-only, sirve para bootstrapear un clon
  nuevo. La app no escribe ahi nunca.
- `db_dir/history/` = ESTADO. Fuera del working tree (`monitor/` esta en .gitignore),
  es donde se acumula. Sobrevive a `git pull`, `git clean` y a un re-clone.

`resolve_read()` implementa la caida: si todavia no hay estado, lee la semilla.
"""
from __future__ import annotations

import os

from config.settings import settings

SEED_DIR = str(settings.history_dir)
STATE_DIR = str(settings.history_state_dir)


def state_path(filename: str) -> str:
    """Ruta de ESCRITURA para `filename` — siempre fuera del working tree."""
    return os.path.join(STATE_DIR, filename)


def resolve_read(path: str) -> str:
    """Ruta de LECTURA: el estado si existe, si no la semilla versionada.

    Devuelve `path` sin tocar cuando no hay ninguno de los dos, para que el
    caller conserve su manejo habitual de "archivo ausente".
    """
    if os.path.isfile(path):
        return path
    seed = os.path.join(SEED_DIR, os.path.basename(path))
    return seed if os.path.isfile(seed) else path
