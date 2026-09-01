"""Shim: las clases de las ON de YPF, sobre el motor de `ingest_on_clases`.

Los dos scripts eran clones de ~100 lineas (leer CSV, matchear por ticker o pata
MEP, escribir `raw_fields.serie_clase`, auditar, backup, commit, reportar). Lo unico
propio de esta fuente es COMO viene el dato: YPF declara vencimiento y cupon juntos
en texto ("Sep-2033 (1,50% / 7,00%)") en vez de una columna dd/mm/aaaa. Eso es hoy
un `Formato` (`FORMATO_YPF`) y el resto vive una sola vez.

Se conserva el comando de siempre porque es el que esta en las notas de operacion.

    py -3.12 scripts/ingest_ypf_clases.py --dry-run
    py -3.12 scripts/ingest_ypf_clases.py
    py -3.12 scripts/ingest_ypf_clases.py --force   # saltea los guards
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.ingest_on_clases import FORMATO_YPF  # noqa: E402
from scripts.ingest_on_clases import main as ingest_clases  # noqa: E402

FUENTE = ROOT / "data" / "iamc" / "ypf_clases.csv"


def main(dry: bool, force: bool = False) -> int:
    return ingest_clases(dry, FUENTE, formato=FORMATO_YPF, force=force)


if __name__ == "__main__":
    raise SystemExit(main("--dry-run" in sys.argv, force="--force" in sys.argv))
