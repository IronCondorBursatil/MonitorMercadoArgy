"""Excel → SQLite (re-seed del catálogo). Correr SOLO cuando edites el master a mano:

    py -3.12 scripts/ingest_master.py            # re-siembra (server apagado)
    py -3.12 scripts/ingest_master.py --force    # saltea guards (server vivo / altas DB-only)

OJO: el re-seed es DESTRUCTIVO (wipe + seed desde el Excel). Guards:
  · server vivo → aborta: el monitor seguiría sirviendo el catálogo VIEJO desde su
    cache en memoria hasta reiniciar (y la DB en uso puede lockear el wipe).
  · altas DB-only → aborta: el Excel es SEMILLA, no espejo — no conoce las altas
    hechas por el ABM (viven solo en SQLite); re-sembrar sin chequear las borraría
    (guard en reseed_with_meta, lista lo que se perdería).
Siempre deja un backup de seguridad del estado actual antes de tocar la DB.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from config.settings import settings  # noqa: E402
from core.infrastructure.db.backup import backup_db  # noqa: E402
from core.infrastructure.db.catalog_repository import ingest_from_excel  # noqa: E402
from scripts.op_guards import server_running  # noqa: E402


def main(argv: list[str]) -> int:
    force = "--force" in argv

    if not force and server_running(settings.host, settings.port):
        print(f"ABORTADO: el monitor está corriendo en {settings.host}:{settings.port}.")
        print("Pará el server primero: el re-seed bajo un server vivo deja el catálogo")
        print("en memoria desactualizado hasta reiniciar. Para forzar: --force")
        return 2

    # Red de seguridad: snapshot del estado ACTUAL antes del wipe destructivo.
    # `tag=` lo hace INCONDICIONAL: el diario del arranque no alcanza (es de la
    # mañana — no contiene las altas ABM del día). Si la DB existe y el snapshot
    # falló, NO hay red → abortar (salvo --force consciente).
    safety = backup_db(settings.catalog_db, settings.backup_dir,
                       keep=settings.backup_keep, tag="pre-reseed")
    if safety:
        print(f"Backup de seguridad: {safety.name}")
    elif Path(settings.catalog_db).is_file():
        print("ABORTADO: no se pudo crear el backup de seguridad pre-reseed "
              "(el wipe quedaría sin red de seguridad).")
        if not force:
            return 3
        print("(--force: continuando SIN backup de seguridad)")

    try:
        n = ingest_from_excel(str(settings.master_xlsx), allow_drop=force)
    except ValueError as e:
        print(f"ABORTADO: {e}")
        return 1
    print(f"Seed OK - {n} instruments -> {settings.catalog_db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
