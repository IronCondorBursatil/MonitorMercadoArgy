import time
from collections import defaultdict

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from apps.web.deps_auth import get_db
from core.infrastructure.db.models import UserORM
from core.security import verify_password, create_access_token, get_password_hash
from apps.web.templates import TEMPLATES as _TEMPLATES
from config.settings import settings

router = APIRouter()

# Rate-limiting en memoria de /login (por-proceso, 1 worker): frena fuerza bruta sin
# dependencia externa. Clave (ip, usuario): un atacante martillando 'admin' se bloquea
# para ese usuario/IP; el resto sigue entrando. Se limpia perezosamente por ventana.
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW_SEC = 300
_login_attempts: dict = defaultdict(list)
# Hash bcrypt dummy: verificar SIEMPRE (aunque el usuario no exista) iguala el tiempo
# de respuesta y evita enumerar usuarios por timing. Lazy para no pagar bcrypt al import.
_DUMMY_HASH = None


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")  # requiere uvicorn --proxy-headers
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = get_password_hash("x")
    return _DUMMY_HASH


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "pages/login.html", {"error": None})

@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    key = (_client_ip(request), (username or "").strip().lower())
    now = time.time()
    recent = [t for t in _login_attempts[key] if now - t < _LOGIN_WINDOW_SEC]
    _login_attempts[key] = recent
    if len(recent) >= _MAX_LOGIN_ATTEMPTS:
        return _TEMPLATES.TemplateResponse(
            request, "pages/login.html",
            {"error": "Demasiados intentos. Esperá unos minutos."}, status_code=429)

    user = db.query(UserORM).filter(UserORM.username == username).first()
    # Verificar SIEMPRE un hash (contra el real o el dummy) → mismo tiempo con/sin usuario.
    ok = verify_password(password, user.hashed_password) if user else verify_password(password, _dummy_hash())
    if not user or not ok:
        _login_attempts[key].append(now)
        return _TEMPLATES.TemplateResponse(request, "pages/login.html", {"error": "Usuario o contraseña incorrectos"})

    _login_attempts.pop(key, None)   # login OK → limpiar el contador

    # Generar token JWT
    access_token = create_access_token(data={"sub": user.username})

    # Redirigir al home (o donde haya intentado entrar) seteando la cookie
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,   # True con TLS → la cookie no viaja por HTTP
        max_age=settings.jwt_access_token_expire_minutes * 60,
    )
    return response

@router.post("/logout")
def logout():
    # POST (no GET) para que no sea CSRF-eable: con la cookie SameSite=Lax, un GET
    # state-changing embebido cross-site igual se dispararía (logout forzado).
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("access_token")
    return response
