import datetime
from typing import Optional

from passlib.context import CryptContext
import jwt

from config.settings import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

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
