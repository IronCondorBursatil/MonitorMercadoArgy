import datetime
import hashlib
import secrets
from typing import Optional

from passlib.context import CryptContext
import jwt

from config.settings import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Centinela "sin contraseña": el usuario invitado existe pero todavía no eligió clave.
# NO es un hash bcrypt: `verify_password` lo rechaza sin lanzar y con el mismo costo.
SIN_PASSWORD_HASH = "!"
_DUMMY_HASH: Optional[str] = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = pwd_context.hash("x")
    return _DUMMY_HASH


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """False para cualquier hash que no sea bcrypt (centinela `!`, vacío, basura), pero
    verificando igual contra un hash dummy para no delatar por tiempo que la cuenta no
    tiene clave. passlib lanzaría ValueError con un hash irreconocible."""
    if not hashed_password or not hashed_password.startswith("$2"):
        pwd_context.verify(plain_password, _dummy_hash())
        return False
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def new_reset_token() -> str:
    """Token de reseteo/invitación: 256 bits, URL-safe. Se persiste SÓLO su hash."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def create_access_token(data: dict, expires_delta: Optional[datetime.timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.datetime.now(datetime.timezone.utc) + expires_delta
    else:
        expire = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=settings.jwt_access_token_expire_minutes)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return encoded_jwt

def decode_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        return payload
    except jwt.PyJWTError:
        return None


# Política de contraseña ÚNICA (la usan la ABM de usuarios y, desde la Fase 2, la
# página pública /reset/{token}). El máximo es el límite REAL de bcrypt: pasados
# 72 bytes trunca EN SILENCIO (passlib con truncate_error=False), o sea que
# "misuperclave...<80 chars>" y sus primeros 72 bytes serían la misma contraseña.
PASSWORD_MIN = 10
PASSWORD_MAX_BYTES = 72


def password_invalida(pw: str) -> Optional[str]:
    """Motivo por el que `pw` no sirve, o None si está bien."""
    if len(pw or "") < PASSWORD_MIN:
        return f"La contraseña tiene que tener al menos {PASSWORD_MIN} caracteres."
    if len((pw or "").encode("utf-8")) > PASSWORD_MAX_BYTES:
        return ("La contraseña supera los 72 bytes: bcrypt trunca en silencio a partir "
                "de ahí, así que el resto no protegería nada.")
    return None
