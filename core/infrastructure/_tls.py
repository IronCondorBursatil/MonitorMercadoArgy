"""Política de verificación TLS por host.

Se **verifica siempre** la cadena TLS (seguro: protege la data de mercado y las
credenciales BYMA de un MITM en redes no confiables). No hay ningún host exceptuado
por default: la allowlist arranca VACÍA.

Historia: hasta 2026-09 el default exceptuaba `open.bymadata.com.ar` y
`addin.bymadata.com.ar` por una cadena rota observada en 2026-06. Re-verificado EN
VIVO el 2026-09-03 con trust store **certifi-only** (el que usa httpx en el droplet
Linux, sin el store del SO), los tres hosts BYMA encadenan bien contra
'GlobalSign RSA OV SSL CA 2018'::

    www.bymadata.com.ar    -> handshake OK, HTTP 405 (respuesta del server)
    open.bymadata.com.ar   -> handshake OK, HTTP 200 /cauciones, 401 /general
    addin.bymadata.com.ar  -> handshake OK, HTTP 401
    (control negativo: self-signed.badssl.com -> CERTIFICATE_VERIFY_FAILED)

Es decir: la excepción quedó obsoleta y mantenerla era degradar la seguridad de dos
hosts que ya no lo necesitan. data912, BCRA, dolarapi, CAFCI, argentinadatos y el
worker REM también verifican OK.

Si alguna cadena vuelve a romperse (renovación fallida, MITM corporativo), la
perilla operativa es el env `MONITOR_TLS_NO_VERIFY_HOSTS` (CSV de hosts) — no
volver a hardcodear `verify=False` en un cliente, que deja este override inerte.
"""

from __future__ import annotations

import os
from typing import Tuple

import httpx

# Allowlist de hosts SIN verificación TLS. VACÍA a propósito: ningún host del repo
# tiene hoy la cadena rota (ver docstring, verificado en vivo 2026-09-03). Para
# exceptuar uno puntualmente se usa `MONITOR_TLS_NO_VERIFY_HOSTS`, no esta tupla.
_DEFAULT_NO_VERIFY: Tuple[str, ...] = ()


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
