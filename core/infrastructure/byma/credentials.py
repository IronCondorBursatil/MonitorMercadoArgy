"""Persistencia de las credenciales BYMA realtime en el `.env` del proyecto.

El usuario las ingresa por la UI; se guardan en `.env` (gitignored — no se comitea)
y se aplican en caliente a `os.environ` para que `BymaRealtimeSource` las tome sin
reiniciar. En el próximo arranque, `config.settings._load_dotenv` las re-carga.

Nota: `.env` vive en la carpeta del proyecto (OneDrive). Se sincroniza a tu nube
personal; mantené tu OneDrive privado. No quedan en git ni en las `.db`.
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
