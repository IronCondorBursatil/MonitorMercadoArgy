import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from apps.web.deps_auth import get_db, get_admin_user_html
from apps.web.templates import TEMPLATES as _TEMPLATES
# email_invalido/normalizar_email: sin uso todavía en este módulo (los consumen los
# handlers POST /users/{id}/datos de las Tasks 6-8); se importan ya para no repetir
# el import en cada task siguiente.
from apps.web.users_service import (TABS, TAB_KEYS, actividad_reciente, resumen,
                                    vista_usuario, email_invalido, normalizar_email)  # noqa: F401
from core.infrastructure.db.models import UserORM
from core.security import get_password_hash, password_invalida

router = APIRouter(dependencies=[Depends(get_admin_user_html)])

# Auditoría de las acciones de admin: quién le hizo qué a quién (journal vía el filtro
# de consola de settings, que deja pasar INFO sólo con `console=True`).
_audit = logging.getLogger("monitor.audit")

# Caracteres que no pueden aparecer en un nombre de usuario legítimo y sí son el
# vector de un XSS almacenado (el username se re-renderiza en /users). Defensa en
# profundidad: el escape correcto vive en la plantilla, esto evita que el payload
# siquiera se persista. Se rechaza también cualquier carácter de control.
_USERNAME_PROHIBIDO = frozenset(["<", ">", chr(34), chr(39), "&", "`", chr(92)])
_USERNAME_MAX = 64
_NOMBRE_MAX = 120
_NOTAS_MAX = 500


def _limpio(v) -> str:
    return "".join(ch for ch in str(v) if ch.isprintable())[:64]


def _username_invalido(username: str) -> str:
    if not username or not username.strip():
        return "El nombre de usuario no puede estar vacío."
    if len(username) > _USERNAME_MAX:
        return f"El nombre de usuario no puede superar {_USERNAME_MAX} caracteres."
    if any(c in _USERNAME_PROHIBIDO or ord(c) < 32 for c in username):
        return "El nombre de usuario tiene caracteres no permitidos."
    return ""


def _texto(v, maximo: int) -> Optional[str]:
    """Campo de texto libre del form: recortado, vacío → None, acotado a `maximo`."""
    s = (v or "").strip()
    return s[:maximo] or None


def _tabs_validas(tabs) -> List[str]:
    return [t for t in (tabs or []) if t in TAB_KEYS]


def _ctx_ficha(u: UserORM) -> dict:
    return {"u": u, "v": vista_usuario(u), "actividad": actividad_reciente(u), "TABS": TABS}


def _users_page(request, db, *, status_code: int = 200, selected_id: Optional[int] = None, **ctx):
    """ÚNICA forma de responder la página: arma todo el contexto (filas, resumen, ficha
    seleccionada). `selected_id` mantiene la ficha abierta tras un POST."""
    users = db.query(UserORM).order_by(UserORM.username).all()
    selected = db.get(UserORM, selected_id) if selected_id is not None else None
    context = {"users": users, "filas": [vista_usuario(u) for u in users],
               "resumen": resumen(users), "TABS": TABS, "selected": selected, **ctx}
    if selected is not None:
        context.update(_ctx_ficha(selected))
    return _TEMPLATES.TemplateResponse(request, "pages/users.html", context,
                                       status_code=status_code)


def _no_existe(request, db, user_id: int):
    """404 explícito: antes se interpolaba `user.username` con `user is None` → 500."""
    return _users_page(request, db, status_code=404,
                       error=f"No existe el usuario id={user_id}.")


@router.get("/users", response_class=HTMLResponse)
def list_users(request: Request, u: Optional[int] = None, db: Session = Depends(get_db)):
    return _users_page(request, db, selected_id=u)


@router.get("/users/{user_id}/ficha", response_class=HTMLResponse)
def ficha(request: Request, user_id: int, db: Session = Depends(get_db)):
    """Fragmento HTMX del panel lateral (la fila de la tabla lo pide con hx-get)."""
    user = db.get(UserORM, user_id)
    if not user:
        return HTMLResponse(f'<div class="msg error">No existe el usuario id={user_id}.</div>',
                            status_code=404)
    return _TEMPLATES.TemplateResponse(request, "fragments/user_ficha.html", _ctx_ficha(user))


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
    invalido = _username_invalido(username) or password_invalida(password)
    if invalido:
        return _users_page(request, db, status_code=400, error=invalido)

    # Check if exists
    existing = db.query(UserORM).filter(UserORM.username == username).first()
    if existing:
        return _users_page(request, db, error=f"El usuario {username} ya existe.")

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

    return _users_page(request, db, success=f"Usuario {username} creado exitosamente.")

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
    invalida = password_invalida(password)
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
