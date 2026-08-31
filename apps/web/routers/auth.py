from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
import datetime

from apps.web.deps_auth import get_db
from core.infrastructure.db.models import UserORM
from core.security import verify_password, create_access_token
from apps.web.templates import TEMPLATES as _TEMPLATES

router = APIRouter()

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "pages/login.html", {"error": None})

@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(UserORM).filter(UserORM.username == username).first()
    
    if not user or not verify_password(password, user.hashed_password):
        return _TEMPLATES.TemplateResponse(request, "pages/login.html", {"error": "Usuario o contraseña incorrectos"})
    
    # Generar token JWT
    access_token = create_access_token(data={"sub": user.username})
    
    # Redirigir al home (o donde haya intentado entrar) seteando la cookie
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key="access_token", 
        value=access_token, 
        httponly=True, 
        samesite="lax",
        max_age=60 * 24 * 7 * 60 # 1 semana en segundos
    )
    return response

@router.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("access_token")
    return response
