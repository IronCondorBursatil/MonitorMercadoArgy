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

@router.post("/users/add", response_class=HTMLResponse)
def add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    is_admin: bool = Form(False),
    tabs: List[str] = Form(default=[]),
    db: Session = Depends(get_db)
):
    invalido = _username_invalido(username)
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

    users = db.query(UserORM).all()
    return _TEMPLATES.TemplateResponse(request, "pages/users.html", {"users": users, "success": f"Usuario {username} creado exitosamente."})

@router.post("/users/delete/{user_id}", response_class=HTMLResponse)
def delete_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = db.query(UserORM).filter(UserORM.id == user_id).first()
    if not user:
        return _no_existe(request, db, user_id)

    # Avoid deleting the last admin
    admins = db.query(UserORM).filter(UserORM.is_admin.is_(True)).count()
    if user.is_admin and admins <= 1:
        return _users_page(request, db, error="No puedes borrar al último administrador.")

    db.delete(user)
    db.commit()
    return _users_page(request, db, success="Usuario borrado.")

@router.post("/users/reset-password/{user_id}", response_class=HTMLResponse)
def reset_password(request: Request, user_id: int, password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(UserORM).filter(UserORM.id == user_id).first()
    if not user:
        return _no_existe(request, db, user_id)

    user.hashed_password = get_password_hash(password)
    db.commit()
    return _users_page(request, db,
                       success=f"Contraseña actualizada para {user.username}.")

@router.post("/users/update/{user_id}", response_class=HTMLResponse)
def update_user(
    request: Request,
    user_id: int,
    is_admin: bool = Form(False),
    tabs: List[str] = Form(default=[]),
    db: Session = Depends(get_db)
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

    user.is_admin = is_admin
    user.allowed_tabs = ["*"] if is_admin else tabs
    db.commit()
    return _users_page(request, db, success=f"Permisos actualizados para {user.username}.")
