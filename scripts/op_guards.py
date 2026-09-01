"""Guards compartidos de los scripts de operación destructivos sobre la catalog.db
(re-seed `ingest_master.py` / restore `restore_catalog.py` / mantenimiento de ONs).

El contrato de detección del monitor vivo vive ACÁ, una sola vez: si la detección
evoluciona (p.ej. probar /api/health en vez de un connect crudo), ambos scripts la
heredan — antes cada uno tenía su copia y podían divergir.

`guard_write` sube un escalón: empaqueta el preflight COMPLETO (server vivo + red de
seguridad) que `ingest_master` tenía inline, para que un script nuevo lo herede en una
línea en vez de re-implementar media versión. Los scripts de ON hacían justamente eso:
llamaban a `backup_db(...)` y descartaban el retorno, así que un rename masivo podía
aplicarse sin respaldo recuperable.
"""

from __future__ import annotations

import socket
from pathlib import Path

from config.settings import settings
from core.infrastructure.db.backup import backup_db


def server_running(host: str, port: int, timeout_s: float = 1.0) -> bool:
    """True si hay algo escuchando en host:port (el monitor vivo). `0.0.0.0` es
    una dirección de bind, no conectable — se prueba por loopback."""
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def guard_write(tag: str, *, force: bool = False) -> int:
    """Preflight de un script que va a ESCRIBIR sobre la catalog.db.

    Devuelve 0 para seguir, o el código con el que hay que abortar (el caller lo
    devuelve tal cual desde `main`). Dos guards, en este orden:

      · server vivo → 2. El monitor mantiene el catálogo en memoria: una escritura
        por debajo no se ve hasta reiniciar y la DB en uso puede lockear.
      · snapshot fallido con una DB existente → 3. `backup_db` devuelve None si la
        copia falló; sin red de seguridad, un rename masivo no se puede deshacer.
        Sin DB previa (bootstrap) no hay nada que perder y sigue.

    El snapshot es INCONDICIONAL (`tag=`): el diario del arranque no alcanza como
    red — es de la mañana y no contiene las altas del ABM del día.

    `force` saltea ambos, para el operador que sabe lo que hace (mismo `--force`
    que `ingest_master`)."""
    if not force and server_running(settings.host, settings.port):
        print(f"ABORTADO: el monitor está corriendo en {settings.host}:{settings.port}.")
        print("Pará el server primero: la escritura no se vería hasta reiniciar (el")
        print("catálogo vive cacheado en memoria). Para forzar: --force")
        return 2

    snap = backup_db(settings.catalog_db, settings.backup_dir,
                     keep=settings.backup_keep, tag=tag)
    if snap:
        print(f"backup pre-op: {snap.name}")
        return 0
    if not Path(settings.catalog_db).is_file():
        return 0   # bootstrap: no hay estado previo que respaldar
    print(f"ABORTADO: no se pudo crear el backup de seguridad '{tag}' "
          "(la escritura quedaría sin red).")
    if not force:
        return 3
    print("(--force: continuando SIN backup de seguridad)")
    return 0
