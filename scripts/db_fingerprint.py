"""Huella de las bases: conteos + sha256 por tabla. Para comparar dos copias.

    py -3.12 scripts/db_fingerprint.py            # legible
    py -3.12 scripts/db_fingerprint.py --json     # para diffear

Existe porque la comparación se venía haciendo a mano, con un script distinto cada
vez. Tiene tres usos concretos:

  * **verificar un backup**: se restaura el bundle en un directorio temporal y se
    diffea contra el vivo. Sin esto, un backup es un archivo del que se sabe que pesa
    lo que tiene que pesar y nada más.
  * **migrar una caja**: fue lo que probó que la copia a Oracle no perdió una fila.
  * **decidir si dos históricos tienen huecos**: `max(day)` por store.

SOLO LECTURA: abre las bases con `mode=ro`. Imprime PRIMERO contra qué `db_dir` corre
— en el servidor `MONITOR_DB_DIR` vive en el drop-in de systemd y una shell manual NO
lo hereda, así que sin ese dato uno compara la base equivocada creyendo que compara
la buena.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Bases y, por cada una, la columna de fecha si tiene (para reportar hasta dónde llega).
_BASES = {
    "catalog.db": None,
    "price_history.db": "day",
    "fci_history.db": "day",
    "ratings_history.db": None,
    "index_history.db": None,
}


def _tablas(con) -> list:
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def huella_de(path: Path, col_fecha=None) -> dict:
    """{tabla: {filas, sha256, ultimo_dia}} de una base."""
    if not path.is_file():
        return {}
    con = sqlite3.connect("file:%s?mode=ro" % path.as_posix(), uri=True)
    try:
        out = {}
        for t in _tablas(con):
            n = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
            h = hashlib.sha256()
            # ORDER BY 1: el orden físico de las filas puede diferir entre una base y
            # su copia (VACUUM, backup online) sin que el CONTENIDO cambie. Sin el
            # orden explícito, dos copias idénticas darían hashes distintos.
            for fila in con.execute('SELECT * FROM "%s" ORDER BY 1' % t):
                h.update(repr(fila).encode())
            entrada = {"filas": n, "sha256": h.hexdigest()[:16]}
            if col_fecha:
                try:
                    entrada["ultimo_dia"] = con.execute(
                        'SELECT MAX("%s") FROM "%s"' % (col_fecha, t)).fetchone()[0]
                except sqlite3.Error:
                    pass
            out[t] = entrada
        return out
    finally:
        con.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db-dir", help="directorio a inspeccionar (default: settings.db_dir)")
    args = ap.parse_args(argv)

    if args.db_dir:
        db_dir = Path(args.db_dir)
    else:
        from config.settings import settings
        db_dir = Path(settings.db_dir)

    datos = {"db_dir": str(db_dir), "bases": {}}
    for nombre, col in _BASES.items():
        datos["bases"][nombre] = huella_de(db_dir / nombre, col)

    if args.json:
        print(json.dumps(datos, indent=2, sort_keys=True))
        return 0

    print("db_dir: %s" % db_dir)
    for nombre, tablas in datos["bases"].items():
        if not tablas:
            print("  %-20s (falta)" % nombre)
            continue
        print("  %s" % nombre)
        for t, d in sorted(tablas.items()):
            extra = "  hasta %s" % d["ultimo_dia"] if d.get("ultimo_dia") else ""
            print("    %-18s %9d filas  %s%s" % (t, d["filas"], d["sha256"], extra))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
