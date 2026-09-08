"""Tokens de reseteo de contraseña e invitación: emisión, validación y consumo (spec
2026-09-08 §3). Acá vive lo que necesita sesión de DB o `request`; los helpers puros
(`new_reset_token`, `hash_token`, `SIN_PASSWORD_HASH`) están en `core/security.py`.

Contrato: token aleatorio de 256 bits, persistido SÓLO como SHA-256; un solo uso; vence
(reset 60 min, invitación 72 h); emitir uno nuevo invalida los vivos del mismo usuario;
un usuario deshabilitado no puede consumirlo; consumirlo sube `token_version` (cierra las
demás sesiones). Nunca se loguea el token."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from config.settings import settings
from core.infrastructure.db.models import PasswordResetTokenORM, UserORM
from core.security import get_password_hash, hash_token, new_reset_token

RESET_TTL = timedelta(minutes=60)
INVITE_TTL = timedelta(hours=72)
_PURGA = timedelta(days=30)   # limpieza perezosa de vencidos viejos, sin loop nuevo


def issue_reset_token(db: Session, user: UserORM, *, purpose: str, channel: str,
                      by: Optional[str]) -> str:
    """Emite un token nuevo para `user` e invalida los que tuviera vivos. Devuelve el token
    en claro UNA vez: el llamador lo muestra o lo manda y no lo guarda."""
    if purpose not in ("reset", "invite"):
        raise ValueError(f"purpose inválido: {purpose!r}")
    now = datetime.now()
    # synchronize_session por default ("evaluate"): filas de PasswordResetTokenORM ya
    # cargadas en ESTA sesión (p. ej. `tokens_de()`/`invitaciones_vivas()` del mismo
    # request) tienen que ver la invalidación acá mismo — `synchronize_session=False`
    # deja el identity map stale y, con `expire_on_commit=False`, ni un re-fetch por PK
    # lo repara (bug real, cubierto por
    # test_issue_invalida_tambien_los_objetos_ya_cargados_en_la_misma_sesion).
    db.query(PasswordResetTokenORM).filter(
        PasswordResetTokenORM.user_id == user.id,
        PasswordResetTokenORM.used_at.is_(None),
    ).update({"used_at": now})
    db.query(PasswordResetTokenORM).filter(
        PasswordResetTokenORM.expires_at < now - _PURGA
    ).delete()
    token = new_reset_token()
    db.add(PasswordResetTokenORM(
        user_id=user.id, token_hash=hash_token(token), purpose=purpose, channel=channel,
        created_at=now, created_by=by,
        expires_at=now + (INVITE_TTL if purpose == "invite" else RESET_TTL),
    ))
    db.commit()
    return token


def lookup_reset_token(db: Session, token: str) -> Optional[tuple[UserORM, PasswordResetTokenORM]]:
    """(usuario, fila) si el token está vivo, no vencido y el usuario activo; si no, None,
    sin distinguir el motivo (la página pública tampoco lo distingue)."""
    if not token or len(token) > 128:
        return None
    row = db.query(PasswordResetTokenORM).filter(
        PasswordResetTokenORM.token_hash == hash_token(token)).first()
    if row is None or row.used_at is not None or row.expires_at <= datetime.now():
        return None
    user = db.get(UserORM, row.user_id)
    if user is None or not user.is_active:
        return None
    return user, row


def consume_reset_token(db: Session, token: str, new_password: str) -> Optional[UserORM]:
    """Cambia la contraseña, marca el token usado y cierra las otras sesiones. La política
    de la contraseña la valida el llamador (`password_invalida`) ANTES de llamar acá."""
    found = lookup_reset_token(db, token)
    if found is None:
        return None
    user, row = found
    now = datetime.now()
    user.hashed_password = get_password_hash(new_password)
    user.password_changed_at = now
    user.token_version = (user.token_version or 0) + 1
    row.used_at = now
    db.commit()
    return user


def reset_link(request, token: str) -> str:
    base = (settings.public_url or str(request.base_url)).rstrip("/")
    return f"{base}/reset/{token}"


def invitaciones_vivas(db: Session) -> dict[int, PasswordResetTokenORM]:
    """{user_id: token} de las invitaciones vivas (para el estado de la tabla del Manager)."""
    now = datetime.now()
    rows = db.query(PasswordResetTokenORM).filter(
        PasswordResetTokenORM.purpose == "invite",
        PasswordResetTokenORM.used_at.is_(None),
        PasswordResetTokenORM.expires_at > now,
    ).all()
    return {r.user_id: r for r in rows}


def tokens_de(db: Session, user: UserORM) -> list[PasswordResetTokenORM]:
    return db.query(PasswordResetTokenORM).filter(
        PasswordResetTokenORM.user_id == user.id
    ).order_by(PasswordResetTokenORM.created_at.desc(), PasswordResetTokenORM.id.desc()).all()
