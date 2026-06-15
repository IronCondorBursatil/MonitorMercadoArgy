"""Guards compartidos de los scripts de operación destructivos sobre la catalog.db
(re-seed `ingest_master.py` / restore `restore_catalog.py`).

El contrato de detección del monitor vivo vive ACÁ, una sola vez: si la detección
evoluciona (p.ej. probar /api/health en vez de un connect crudo), ambos scripts la
heredan — antes cada uno tenía su copia y podían divergir.
"""

from __future__ import annotations

import socket


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
