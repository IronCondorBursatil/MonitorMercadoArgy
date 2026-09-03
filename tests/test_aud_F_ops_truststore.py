"""Auditoría F_ops — `scripts/byma_enrich_seed_isin.py` no era ejecutable.

Hacía `import truststore` + `truststore.inject_into_ssl()` a nivel de módulo SIN
guard, y `truststore` no está declarado en requirements.txt / .lock / -dev.txt ni
instalado en el intérprete del proyecto. Correrlo daba `ModuleNotFoundError` antes de
hacer nada, acá y en el droplet.

El primer arreglo (F_ops) fue envolverlo en `try/except ImportError`, copiando lo que
entonces hacía el camino de producción (`byma/catalog_enrich.py`). **Ese guard ya no
existe**: la re-auditoría R2 borró el import de `catalog_enrich` —era código muerto
permanente que el `except` disimulaba— y el cierre Z3 hizo lo mismo acá, dejando la
Session del script con la política TLS única del repo (`_tls.should_verify`). Este test
sobrevive como regresión mínima del hallazgo original: el script tiene que **importarse
sin truststore instalado**, que es la máquina real. Lo demás (que no vuelva el import,
que la Session siga la política, que `main()` use el helper) lo cubre
`tests/test_fin_Z3_ops_byma_seed_tls.py`.
"""

from __future__ import annotations

import importlib


def test_el_script_importa_sin_truststore_instalado():
    # Sin guard ni import: ahora la condición vale con truststore instalado o no, así
    # que el test dejó de necesitar el skip que tenía cuando dependía del `except`.
    mod = importlib.import_module("scripts.byma_enrich_seed_isin")

    assert hasattr(mod, "ficha"), "el módulo cargó pero sin su API"
