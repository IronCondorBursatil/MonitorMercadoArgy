"""Política de verificación TLS por host.

Por defecto se **verifica** la cadena TLS (seguro: protege la data de mercado de
un MITM en redes no confiables). Solo se saltea la verificación para los hosts cuya
cadena está demostrablemente rota.

Verificado en vivo (2026-06): únicamente los endpoints `open.bymadata.com.ar` y
`addin.bymadata.com.ar` de BYMA fallan con CERTIFICATE_VERIFY_FAILED. data912, BCRA,
dolarapi, CAFCI, argentinadatos y el worker REM verifican OK — el comentario viejo
de `_http.py` que los marcaba "rotos" estaba desactualizado (sus cadenas se arreglaron).

Override por env `MONITOR_TLS_NO_VERIFY_HOSTS` (CSV de hosts), p.ej. para agregar un
host si alguna cadena se rompe en el futuro, o vaciarlo para forzar verify en todo.
"""

from __future__ import annotations

import os
from typing import Tuple

import httpx

# Allowlist de hosts SIN verificación TLS (cadena rota verificada). Default mínimo:
# solo los dos endpoints BYMA que lo necesitan.
_DEFAULT_NO_VERIFY: Tuple[str, ...] = ("open.bymadata.com.ar", "addin.bymadata.com.ar")


def no_verify_hosts() -> Tuple[str, ...]:
    """Hosts (lowercase) para los que se saltea la verificación TLS."""
    raw = os.environ.get("MONITOR_TLS_NO_VERIFY_HOSTS")
    if raw is None:
        return _DEFAULT_NO_VERIFY
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def should_verify(url: str) -> bool:
    """True si hay que verificar TLS para `url` (default seguro); False si su host
    está en la allowlist de cadena-rota. URLs sin host (relativas/vacías) → True."""
    try:
        host = (httpx.URL(url).host or "").lower()
    except Exception:  # noqa: BLE001 — URL inválida → verificar (seguro)
        return True
    if not host:
        return True
    return host not in no_verify_hosts()
