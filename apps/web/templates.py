"""Singleton de Jinja2Templates compartido por todos los routers.

Evita que cada router construya su propia instancia apuntando al mismo
directorio — misma ruta resuelta que el patrón anterior (parent.parent/templates).
"""

from pathlib import Path
from typing import Any, Mapping

from fastapi import Request
from fastapi.templating import Jinja2Templates

from apps.web.deps_auth import _UNRESOLVED, _get_user_from_token, user_can_tab
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
            context["current_user"] = user = self._resolve_user(request)
            context["has_tab"] = lambda tab: user_can_tab(user, tab)

        return super().TemplateResponse(
            request, name, context, status_code, headers, media_type, background
        )

    @staticmethod
    def _resolve_user(request: Request):
        """Usuario del request, SIN volver a tocar la DB si ya lo resolvio la
        dependencia de auth (`deps_auth._publish` lo deja en `request.state`).

        Antes se abria una `SessionLocal()` y se decodificaba el JWT en cada
        respuesta de template: el dashboard pide ~14 fragmentos cada 5s por cliente,
        o sea 14 sesiones SQLite + 14 decodes de mas por ciclo y por cliente.

        El camino viejo queda de FALLBACK para las respuestas sin dependencia de
        auth (`/login`) — y es tambien el que ejercita la fixture `_auth_bypass` de
        `tests/conftest.py`, cuyos overrides de dependencia no reciben el `request`
        y por eso nunca publican `request.state.current_user`.
        """
        user = getattr(request.state, "current_user", _UNRESOLVED)
        if user is not _UNRESOLVED:
            return user
        db = SessionLocal()
        try:
            return _get_user_from_token(request, db)
        finally:
            db.close()

TEMPLATES = AuthJinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
