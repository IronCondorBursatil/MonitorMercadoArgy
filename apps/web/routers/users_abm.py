import logging
from typing import List
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from apps.web.deps_auth import get_db, get_admin_user_html
from core.infrastructure.db.models import UserORM
from core.security import get_password_hash
from apps.web.templates import TEMPLATES as _TEMPLATES

router = APIRouter(dependencies=[Depends(get_admin_user_html)])

# Caracteres que no pueden aparecer en un nombre de usuario legítimo y sí son el
# vector de un XSS almacenado (el username se re-renderiza en /users). Defensa en
# profundidad: el escape correcto vive en la plantilla, esto evita que el payload
# siquiera se persista. Se rechaza también cualquier carácter de control.
_USERNAME_PROHIBIDO = frozenset(["<", ">", chr(34), chr(39), "&", "`", chr(92)])
_USERNAME_MAX = 64


def _users_page(request, db, *, status_code: int = 200, **ctx):
    users = db.query(UserORM).all()
    return _TEMPLATES.TemplateResponse(request, "pages/users.html",
                                       {"users": users, **ctx}, status_code=status_code)


def _no_existe(request, db, user_id: int):
    """404 explícito: antes se interpolaba `user.username` con `user is None` → 500."""
    return _users_page(request, db, status_code=404,
                       error=f"No existe el usuario id={user_id}.")


def _username_invalido(username: str) -> str:
    if not username or not username.strip():
        return "El nombre de usuario no puede estar vacío."
    if len(username) > _USERNAME_MAX:
        return f"El nombre de usuario no puede superar {_USERNAME_MAX} caracteres."
    if any(c in _USERNAME_PROHIBIDO or ord(c) < 32 for c in username):
        return "El nombre de usuario tiene caracteres no permitidos."
    return ""

@router.get("/users", response_class=HTMLResponse)
def list_users(request: Request, db: Session = Depends(get_db)):
    users = db.query(UserORM).all()
    return _TEMPLATES.TemplateResponse(request, "pages/users.html", {"users": users})

# Auditoria de las acciones de admin: quien le hizo que a quien. No habia NADA — ni
# un logger en este modulo — asi que un alta, un borrado o un reset de contrasena no
# dejaban rastro en ningun lado.
_audit = logging.getLogger("monitor.audit")


def _limpio(v) -> str:
    return "".join(ch for ch in str(v) if ch.isprintable())[:64]


# Minimo de contrasena. El generador de la UI hace 12 chars; esto acota lo que se
# tipea a mano. El maximo es el limite REAL de bcrypt: pasados 72 bytes trunca EN
# SILENCIO (passlib con truncate_error=False), o sea que "misuperclave...<80 chars>"
# y sus primeros 72 bytes serian la misma contrasena.
_PASSWORD_MIN = 10
_PASSWORD_MAX_BYTES = 72


def _password_invalida(pw: str):
    """Motivo por el que `pw` no sirve, o None si esta bien."""
    if len(pw or "") < _PASSWORD_MIN:
        return f"La contrasena tiene que tener al menos {_PASSWORD_MIN} caracteres."
    if len((pw or "").encode("utf-8")) > _PASSWORD_MAX_BYTES:
        return ("La contrasena supera los 72 bytes: bcrypt trunca en silencio a partir "
                "de ahi, asi que el resto no protegeria nada.")
    return None


@router.post("/users/add", response_class=HTMLResponse)
def add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    is_admin: bool = Form(False),
    tabs: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
    admin: UserORM = Depends(get_admin_user_html),
):
    invalido = _username_invalido(username) or _password_invalida(password)
    if invalido:
        return _users_page(request, db, status_code=400, error=invalido)

    # Check if exists
    existing = db.query(UserORM).filter(UserORM.username == username).first()
    if existing:
        users = db.query(UserORM).all()
        return _TEMPLATES.TemplateResponse(request, "pages/users.html", {"users": users, "error": f"El usuario {username} ya existe."})

    new_user = UserORM(
        username=username,
        hashed_password=get_password_hash(password),
        is_admin=is_admin,
        allowed_tabs=["*"] if is_admin else tabs
    )
    db.add(new_user)
    db.commit()
    _audit.info("users action=add by=%s target=%s is_admin=%s tabs=%s",
                _limpio(getattr(admin, "username", "?")), _limpio(username),
                bool(is_admin), _limpio(",".join(tabs or [])), extra={"console": True})

    users = db.query(UserORM).all()
    return _TEMPLATES.TemplateResponse(request, "pages/users.html", {"users": users, "success": f"Usuario {username} creado exitosamente."})

@router.post("/users/delete/{user_id}", response_class=HTMLResponse)
def delete_user(request: Request, user_id: int, db: Session = Depends(get_db),
                admin: UserORM = Depends(get_admin_user_html)):
    user = db.query(UserORM).filter(UserORM.id == user_id).first()
    if not user:
        return _no_existe(request, db, user_id)

    # Avoid deleting the last admin
    admins = db.query(UserORM).filter(UserORM.is_admin.is_(True)).count()
    if user.is_admin and admins <= 1:
        return _users_page(request, db, error="No puedes borrar al último administrador.")

    borrado = user.username        # antes del delete: despues el objeto esta expirado
    db.delete(user)
    db.commit()
    # El usuario resuelto por la dependencia de auth queda publicado en `request.state`
    # y el contexto de los templates lo lee de ahí. Si el admin se borró a SÍ MISMO, ese
    # objeto es justo el que acabamos de borrar —y con `expire_on_commit=False` conserva
    # sus atributos—, así que esta misma respuesta se renderizaba como una sesión viva.
    # Limpiándolo, `templates._resolve_user` cae al camino de siempre: consulta la DB, no
    # encuentra la fila y el nav sale vacío. (El request SIGUIENTE ya iba a 302 /login: lo
    # que se arregla acá es que la respuesta no mienta sobre el estado de la cuenta.)
    actual = getattr(request.state, "current_user", None)
    if actual is not None and getattr(actual, "id", None) == user_id:
        request.state.current_user = None
    _audit.info("users action=delete by=%s target=%s",
                _limpio(getattr(admin, "username", "?")), _limpio(borrado),
                extra={"console": True})
    return _users_page(request, db, success="Usuario borrado.")

@router.post("/users/reset-password/{user_id}", response_class=HTMLResponse)
def reset_password(request: Request, user_id: int, password: str = Form(...),
                   db: Session = Depends(get_db),
                   admin: UserORM = Depends(get_admin_user_html)):
    user = db.query(UserORM).filter(UserORM.id == user_id).first()
    if not user:
        return _no_existe(request, db, user_id)
    # La validacion va DESPUES del lookup a proposito: un id inexistente tiene que dar
    # 404 aunque la contrasena tambien sea invalida (lo fija test_aud_D1).
    invalida = _password_invalida(password)
    if invalida:
        return _users_page(request, db, status_code=400, error=invalida)

    user.hashed_password = get_password_hash(password)
    # Cierra las sesiones abiertas de ese usuario. NO se hace en `update_user`: los
    # permisos se releen de la base en cada request, asi que una degradacion ya es
    # inmediata y bumpear ahi solo desloguearia gente sin comprar nada.
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    _audit.info("users action=reset_password by=%s target=%s",
                _limpio(getattr(admin, "username", "?")), _limpio(user.username),
                extra={"console": True})
    return _users_page(request, db,
                       success=f"Contraseña actualizada para {user.username}.")

@router.post("/users/update/{user_id}", response_class=HTMLResponse)
def update_user(
    request: Request,
    user_id: int,
    is_admin: bool = Form(False),
    tabs: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
    admin: UserORM = Depends(get_admin_user_html),
):
    user = db.query(UserORM).filter(UserORM.id == user_id).first()
    if not user:
        return _no_existe(request, db, user_id)

    # Avoid removing admin from the last admin
    if user.is_admin and not is_admin:
        admins = db.query(UserORM).filter(UserORM.is_admin.is_(True)).count()
        if admins <= 1:
            return _users_page(
                request, db,
                error="No puedes quitarle el rol de admin al último administrador.")

    antes_admin, antes_tabs = user.is_admin, list(user.allowed_tabs or [])
    user.is_admin = is_admin
    user.allowed_tabs = ["*"] if is_admin else tabs
    db.commit()
    # La PROMOCION A ADMIN es la accion mas sensible de toda la ABM y era la unica de
    # los cuatro handlers que no quedaba registrada en ningun lado (auditoria
    # 2026-09-04). Se loguea el estado ANTES y DESPUES: "quien tenia que rol" es
    # justo lo que se quiere reconstruir despues de un incidente.
    _audit.info("users action=update by=%s target=%s is_admin=%s->%s tabs=%s->%s",
                _limpio(getattr(admin, "username", "?")), _limpio(user.username),
                bool(antes_admin), bool(is_admin),
                _limpio(",".join(antes_tabs)),
                _limpio(",".join(user.allowed_tabs or [])), extra={"console": True})
    return _users_page(request, db, success=f"Permisos actualizados para {user.username}.")
