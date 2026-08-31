"""Singleton de Jinja2Templates compartido por todos los routers.

Evita que cada router construya su propia instancia apuntando al mismo
directorio — misma ruta resuelta que el patrón anterior (parent.parent/templates).
"""

from pathlib import Path
from typing import Any, Mapping

from fastapi import Request
from fastapi.templating import Jinja2Templates

from apps.web.deps_auth import _get_user_from_token
from core.infrastructure.db.engine import SessionLocal

class AuthJinja2Templates(Jinja2Templates):
    def TemplateResponse(
        self,
        request: Request,
        name: str,
        context: dict[str, Any] | None = None,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: Any | None = None,
    ):
        if context is None:
            context = {}

        if "current_user" not in context:
            db = SessionLocal()
            try:
                user = _get_user_from_token(request, db)
                context["current_user"] = user
                context["has_tab"] = lambda tab: user and (user.is_admin or "*" in (user.allowed_tabs or []) or tab in (user.allowed_tabs or []))
            finally:
                db.close()

        return super().TemplateResponse(
            request, name, context, status_code, headers, media_type, background
        )

TEMPLATES = AuthJinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
