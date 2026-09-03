"""Persistencia de las credenciales BYMA realtime en el `.env` del proyecto.

El usuario las ingresa por la UI; se guardan en `.env` (gitignored — no se comitea)
y se aplican en caliente a `os.environ` para que `BymaRealtimeSource` las tome sin
reiniciar. En el próximo arranque, `config.settings._load_dotenv` las re-carga.

Nota: `.env` vive en la carpeta del proyecto y está gitignoreado — no viaja al repo
ni queda en las `.db`. En el servidor va en el EnvironmentFile del servicio.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

USER_KEY = "BYMADATA_USER"
PASS_KEY = "BYMADATA_PASS"
_KEYS = (USER_KEY, PASS_KEY)

# El `.env` es un formato de una linea por registro: cualquier separador de linea
# embebido en una credencial inyecta una linea KEY=VALUE arbitraria (que
# `config.settings._load_dotenv` carga a os.environ en el proximo arranque y que
# `clear_credentials` no limpia). Se rechaza, no se sanea en silencio.
# Ojo: la lista NO es solo \n/\r. `str.splitlines()` -- que usa el round-trip de
# esta misma funcion al releer el archivo -- corta tambien en VT/FF/FS/GS/RS,
# NEL (U+0085) y LS/PS (U+2028/9). Se agrega NUL, que rompe os.environ.
_FORBIDDEN = "".join(map(chr, (0x00, 0x0A, 0x0B, 0x0C, 0x0D,
                               0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029)))


def env_path(path: Optional[os.PathLike] = None) -> Path:
    if path is not None:
        return Path(path)
    from config.settings import settings
    return settings.base_dir / ".env"


def _parse_key(line: str) -> Optional[str]:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    return s.split("=", 1)[0].strip()


def save_credentials(user: str, password: str, path: Optional[os.PathLike] = None) -> None:
    """Valida no-vacíos, setea os.environ y hace upsert de las 2 claves en `.env`
    (preservando el resto del archivo)."""
    user = (user or "").strip()
    password = (password or "").strip()
    if not user or not password:
        raise ValueError("Usuario y contraseña son requeridos.")
    if any(c in user or c in password for c in _FORBIDDEN) or "=" in user:
        # Contrato: se PROPAGA ValueError; traducirlo a una respuesta es del caller.
        # Hoy el caller SÍ lo traduce: `apps/web/routers/source.py::source_credentials`
        # envuelve la llamada en `try/except ValueError` y devuelve 400 con el motivo
        # (antes la llamada estaba fuera de todo try —el único envolvía el probe
        # `_ensure_token`— y, como `apps/web/app.py` no registra
        # `exception_handler(ValueError)`, este camino terminaba en un 500 con traza).
        # No es inalcanzable: un usuario BYMA con `=` en el nombre pasa el probe OAuth
        # (login válido) y recién muere acá.
        # STATUS-HTTP-REAL: 400  ← lo verifica tests/test_rem_R2_infra_credentials_contrato.py
        # contra el AST del router; si alguien saca ese try/except, el test exige
        # volver este número a 500 (y así el comentario no vuelve a mentir).
        raise ValueError("Usuario/contraseña con caracteres no permitidos.")
    os.environ[USER_KEY] = user
    os.environ[PASS_KEY] = password

    kv = {USER_KEY: user, PASS_KEY: password}
    p = env_path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
    out, seen = [], set()
    for line in lines:
        k = _parse_key(line)
        if k in kv:
            if k in seen:
                continue  # .env con clave duplicada → conservar solo la 1ª
            out.append(f"{k}={kv[k]}")
            seen.add(k)
        else:
            out.append(line)
    for k, v in kv.items():
        if k not in seen:
            out.append(f"{k}={v}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    logger.info("BYMA credenciales guardadas en %s (usuario=%s).", p.name, user)


def clear_credentials(path: Optional[os.PathLike] = None) -> None:
    """Saca las credenciales de os.environ y del `.env`."""
    for k in _KEYS:
        os.environ.pop(k, None)
    p = env_path(path)
    if not p.is_file():
        return
    kept = [line for line in p.read_text(encoding="utf-8").splitlines()
            if _parse_key(line) not in set(_KEYS)]
    p.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    logger.info("BYMA credenciales borradas de %s.", p.name)
