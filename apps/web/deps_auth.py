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

def _get_user_from_token(request: Request, db: Session) -> Optional[UserORM]:
    token = request.cookies.get("access_token")
    if not token:
        # Check authorization header as fallback for API clients
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

    if not token:
        return None

    payload = decode_access_token(token)
    if not payload:
        return None

    username: str = payload.get("sub")
    if username is None:
        return None

    user = db.query(UserORM).filter(UserORM.username == username).first()
    return user

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
        if current_user.is_admin:
            return current_user

        tabs = current_user.allowed_tabs or []
        if "*" in tabs or self.tab_name in tabs:
            return current_user

        # Autenticado pero sin esta pestaña: 403 (no 302 a /login). Con el redirect,
        # tipear a mano una URL sin permiso te devolvía el FORMULARIO DE LOGIN estando
        # logueado —indistinguible de una sesión vencida— y, si el landing del usuario
        # no era esa URL, quedaba pareciendo un rebote.
        raise TabForbiddenException(self.tab_name, tabs)

