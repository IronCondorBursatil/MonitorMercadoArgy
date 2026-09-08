"""Reglas PURAS del Manager de usuarios (sin FastAPI ni sesión de DB): normalización y
validación de email, estado derivado, actividad reciente y formato de fechas. Las
prueba `tests/test_users_manager.py` sin levantar la app. El router
(`routers/users_abm.py`) sólo orquesta.

Spec: docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md §2.3, §6."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from core.infrastructure.db.models import UserORM

# Pestañas en el MISMO orden que el nav de base.html y que `_TAB_LANDING` (auth.py).
TABS: tuple[tuple[str, str], ...] = (
    ("bonos", "Bonos"), ("on", "O.N's"), ("curva", "Curva"), ("cartera", "Cartera"),
    ("bcra", "BCRA"), ("cashflows", "Cashflows"), ("fci", "FCI"),
    ("escenarios", "Escenarios"), ("opciones", "Opciones"), ("catalogo", "Catálogo"),
    ("abm", "ABM Bonos"),
)
TAB_KEYS = frozenset(k for k, _ in TABS)

_EMAIL_MAX = 254
# Los mismos caracteres que rechaza el username (defensa en profundidad contra XSS
# almacenado: el email se re-renderiza en /users) + espacio y coma, que un email
# válido nunca lleva.
_EMAIL_PROHIBIDO = frozenset(["<", ">", '"', "'", "&", "`", "\\", " ", ","])
_MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def normalizar_email(raw) -> Optional[str]:
    """'' / None → None (sin email); el resto en minúsculas y sin espacios en los bordes."""
    s = (raw or "").strip().lower()
    return s or None


def email_invalido(email: Optional[str]) -> str:
    """Motivo por el que `email` no sirve, o '' si está bien. `None` = sin email = válido."""
    if email is None:
        return ""
    if len(email) > _EMAIL_MAX:
        return f"El email no puede superar {_EMAIL_MAX} caracteres."
    if any(c in _EMAIL_PROHIBIDO or ord(c) < 32 for c in email):
        return "El email tiene caracteres no permitidos."
    local, sep, dominio = email.partition("@")
    if (not sep or not local or "@" in dominio or "." not in dominio
            or dominio.startswith(".") or dominio.endswith(".")):
        return "El email no tiene un formato válido (nombre@dominio)."
    return ""


def iniciales(u: UserORM) -> str:
    partes = (u.full_name or u.username or "?").split()
    return "".join(p[0] for p in partes[:2]).upper()


def fmt_momento(dt: Optional[datetime], hoy: Optional[date] = None) -> str:
    """'hoy 09:12' · 'ayer 18:40' · '12 ago' · '03 sep 2025' (otro año) · '—' sin dato."""
    if dt is None:
        return "—"
    hoy = hoy or date.today()
    d = dt.date()
    if d == hoy:
        return f"hoy {dt:%H:%M}"
    if (hoy - d).days == 1:
        return f"ayer {dt:%H:%M}"
    if d.year == hoy.year:
        return f"{d.day:02d} {_MESES[d.month - 1]}"
    return f"{d.day:02d} {_MESES[d.month - 1]} {d.year}"


def estado_usuario(u: UserORM) -> str:
    return "activo" if u.is_active else "deshabilitado"


def vista_usuario(u: UserORM, hoy: Optional[date] = None) -> dict:
    """Lo que la tabla y la cabecera de la ficha muestran de un usuario."""
    return {
        "u": u,
        "iniciales": iniciales(u),
        "estado": estado_usuario(u),
        "sin_email": not u.email,
        "ultimo_acceso": fmt_momento(u.last_login_at, hoy),
        "pwd_cambiada": fmt_momento(u.password_changed_at, hoy),
        "alta": fmt_momento(u.created_at, hoy),
        "tabs": [] if u.is_admin else [t for t in (u.allowed_tabs or []) if t != "*"],
    }


def actividad_reciente(u: UserORM, hoy: Optional[date] = None) -> list[dict]:
    """Eventos DERIVADOS de las columnas (sin tabla de eventos), del más nuevo al más viejo.
    La Fase 2 suma los tokens (link emitido / consumido / invitación aceptada)."""
    ev: list[dict] = []
    if u.last_login_at:
        ev.append({"cuando": u.last_login_at, "que": "Ingreso", "detalle": u.last_login_ip or ""})
    if u.password_changed_at:
        ev.append({"cuando": u.password_changed_at, "que": "Contraseña cambiada",
                   "detalle": "por un administrador"})
    if u.created_at:
        ev.append({"cuando": u.created_at, "que": "Alta",
                   "detalle": f"por {u.created_by}" if u.created_by else ""})
    ev.sort(key=lambda e: e["cuando"], reverse=True)
    for e in ev:
        e["texto"] = fmt_momento(e["cuando"], hoy)
    return ev


def resumen(users) -> dict:
    return {
        "total": len(users),
        "activos": sum(1 for u in users if u.is_active),
        "deshabilitados": sum(1 for u in users if not u.is_active),
        "sin_email": sum(1 for u in users if not u.email),
    }
