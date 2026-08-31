from typing import List
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from apps.web.deps_auth import get_db, get_admin_user_html
from core.infrastructure.db.models import UserORM
from core.security import get_password_hash
from apps.web.templates import TEMPLATES as _TEMPLATES

router = APIRouter(dependencies=[Depends(get_admin_user_html)])

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
    if user:
        # Avoid deleting the last admin
        admins = db.query(UserORM).filter(UserORM.is_admin.is_(True)).count()
        if user.is_admin and admins <= 1:
            users = db.query(UserORM).all()
            return _TEMPLATES.TemplateResponse(request, "pages/users.html", {"users": users, "error": "No puedes borrar al último administrador."})

        db.delete(user)
        db.commit()

    users = db.query(UserORM).all()
    return _TEMPLATES.TemplateResponse(request, "pages/users.html", {"users": users, "success": "Usuario borrado."})

@router.post("/users/reset-password/{user_id}", response_class=HTMLResponse)
def reset_password(request: Request, user_id: int, password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(UserORM).filter(UserORM.id == user_id).first()
    if user:
        user.hashed_password = get_password_hash(password)
        db.commit()

    users = db.query(UserORM).all()
    return _TEMPLATES.TemplateResponse(request, "pages/users.html", {"users": users, "success": f"Contraseña actualizada para {user.username}."})

@router.post("/users/update/{user_id}", response_class=HTMLResponse)
def update_user(
    request: Request,
    user_id: int,
    is_admin: bool = Form(False),
    tabs: List[str] = Form(default=[]),
    db: Session = Depends(get_db)
):
    user = db.query(UserORM).filter(UserORM.id == user_id).first()
    if user:
        # Avoid removing admin from the last admin
        if user.is_admin and not is_admin:
            admins = db.query(UserORM).filter(UserORM.is_admin.is_(True)).count()
            if admins <= 1:
                users = db.query(UserORM).all()
                return _TEMPLATES.TemplateResponse(request, "pages/users.html", {"users": users, "error": "No puedes quitarle el rol de admin al último administrador."})

        user.is_admin = is_admin
        user.allowed_tabs = ["*"] if is_admin else tabs
        db.commit()

    users = db.query(UserORM).all()
    return _TEMPLATES.TemplateResponse(request, "pages/users.html", {"users": users, "success": f"Permisos actualizados para {user.username}."})
