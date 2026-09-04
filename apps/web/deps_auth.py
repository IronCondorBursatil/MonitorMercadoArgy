from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

class RequiresLoginException(Exception):
    """NO autenticado (sin cookie / token vencido) → el handler global redirige a /login."""
    pass


class TabForbiddenException(Exception):
    """Autenticado pero SIN permiso para esa pestaña.

    Es un caso DISTINTO de la falta de login y por eso tiene excepción propia: mandar
    a /login a alguien que YA está logueado le miente ('sesión vencida'), lo hace
    reingresar la clave y —si su pestaña de landing no es la que tecleó— parece un
    rebote sin explicación. Corresponde 403 con la lista de lo que sí puede ver.
    """

    def __init__(self, tab: str, allowed=None):
        self.tab = tab
        self.allowed = list(allowed or [])
        super().__init__(f"sin permiso para la pestaña {tab!r}")


from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import UserORM
from core.security import decode_access_token

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Centinela: distingue "no hay usuario" (None, resuelto) de "todavia no se resolvio".
_UNRESOLVED = object()


def user_can_tab(user: Optional[UserORM], tab: str) -> bool:
    """Permiso por pestana. Fuente unica de la regla (la usan RequireTabPermission
    y el `has_tab()` del contexto de los templates)."""
    if user is None:
        return False
    if user.is_admin:
        return True
    tabs = user.allowed_tabs or []
    return "*" in tabs or tab in tabs


def _publish(request: Request, user: Optional[UserORM]) -> Optional[UserORM]:
    """Publica el usuario resuelto en `request.state` para el resto del request.

    Sin esto, `apps/web/templates.py` volvia a abrir una `SessionLocal()` y a
    decodificar el JWT en CADA respuesta de template, ADEMAS de la que abrio esta
    dependencia: 2 sesiones SQLite + 2 decodes por fragmento, y el dashboard pide
    ~14 fragmentos cada 5s por cliente.
    """
    request.state.current_user = user
    return user


def _get_user_from_token(request: Request, db: Session) -> Optional[UserORM]:
    token = request.cookies.get("access_token")
    if not token:
        # Check authorization header as fallback for API clients
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

    if not token:
        return _publish(request, None)

    payload = decode_access_token(token)
    if not payload:
        return _publish(request, None)

    username: str = payload.get("sub")
    if username is None:
        return _publish(request, None)

    user = db.query(UserORM).filter(UserORM.username == username).first()
    return _publish(request, user)

def get_current_user(request: Request, db: Session = Depends(get_db)) -> UserORM:
    """Para APIs: devuelve 401 si no está logueado."""
    user = _get_user_from_token(request, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user

def get_current_user_html(request: Request, db: Session = Depends(get_db)) -> UserORM:
    """Para vistas HTMX/HTML: redirige al login si no está logueado."""
    user = _get_user_from_token(request, db)
    if not user:
        raise RequiresLoginException()
    return user

def get_admin_user(current_user: UserORM = Depends(get_current_user)) -> UserORM:
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The user doesn't have enough privileges"
        )
    return current_user

def get_admin_user_html(current_user: UserORM = Depends(get_current_user_html)) -> UserORM:
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No admin")
    return current_user

class RequireTabPermission:
    def __init__(self, tab_name: str):
        self.tab_name = tab_name

    def __call__(self, current_user: UserORM = Depends(get_current_user_html)):
        if user_can_tab(current_user, self.tab_name):
            return current_user

        tabs = current_user.allowed_tabs or []

        # Autenticado pero sin esta pestaña: 403 (no 302 a /login). Con el redirect,
        # tipear a mano una URL sin permiso te devolvía el FORMULARIO DE LOGIN estando
        # logueado —indistinguible de una sesión vencida— y, si el landing del usuario
        # no era esa URL, quedaba pareciendo un rebote.
        raise TabForbiddenException(self.tab_name, tabs)

