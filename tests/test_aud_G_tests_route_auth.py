"""Auditoría G_tests — "toda ruta exige login" tiene que ser una propiedad
VERIFICADA, no una convención.

`tests/conftest.py::_auth_bypass` es autouse: todo test que no marque `noauth`
corre como admin. Si mañana se agrega `app.include_router(nuevo.router)` sin el
`dependencies=[Depends(RequireTabPermission(...))]` que llevan los 12 routers de
`apps/web/app.py`, los tests del router nuevo pasan igual (bypass), el gate queda
verde y la ruta queda abierta en el droplet. Este módulo camina `app.routes` y
falla si aparece una ruta sin dependencia de auth.

Las rutas de FastAPI que NO son `APIRoute` (las docs de OpenAPI) no tienen árbol
de dependencias: se cuentan como VIOLACIÓN, no se saltean — saltearlas fue el
agujero del fix ingenuo (`route.dependant.dependencies` no existe ahí).
"""

import os

import pytest
from fastapi import Depends, FastAPI
from starlette.routing import Mount

from tests._routes import iter_app_routes

# Nombres (qualname) que marcan una dependencia de autenticación.
_AUTH_MARKERS = ("get_current_user", "get_admin_user", "RequireTabPermission")

# Rutas conscientemente PÚBLICAS. Cualquier otra sin auth es un bug.
#   /login  — el formulario y el POST de login (obvio).
#   /logout — solo borra la cookie.
#   /api/health — probe externo; recortado a propósito (sin last_error).
_PUBLIC_PATHS = {"/login", "/logout", "/api/health"}
# Mounts públicos: assets estáticos.
_PUBLIC_MOUNTS = {"/static"}
# Docs de OpenAPI: apagadas por default en apps/web/app.py; si alguien las prende
# en desarrollo con MONITOR_ENABLE_DOCS=1, son públicas a sabiendas (nunca en prod).
_DOCS_PATHS = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}


def _auth_dep_names(dependant, acc=None):
    """Qualnames de TODO el árbol de dependencias de una ruta (recursivo)."""
    acc = set() if acc is None else acc
    for sub in dependant.dependencies:
        call = sub.call
        acc.add(getattr(call, "__qualname__", None) or type(call).__name__)
        _auth_dep_names(sub, acc)
    return acc


def routes_without_auth(app):
    """Rutas de `app` que no exigen autenticación (excluye la allowlist pública)."""
    allowed = set(_PUBLIC_PATHS)
    if os.environ.get("MONITOR_ENABLE_DOCS"):
        allowed |= _DOCS_PATHS
    offenders = []
    # `iter_app_routes` aplana los wrappers `_IncludedRouter` de FastAPI >= 0.141 (la
    # versión que corre el servidor) y devuelve aparte las dependencias que se pasaron
    # en `include_router(..., dependencies=[...])`, que en esa versión NO quedan en el
    # `dependant` de la ruta. Sin eso este guard ve 4 rutas en vez de 66 — ver
    # `tests/_routes.py`.
    for route, heredadas in iter_app_routes(app):
        path = getattr(route, "path", None) or getattr(route, "path_format", "?")
        if isinstance(route, Mount):
            if path not in _PUBLIC_MOUNTS:
                offenders.append((path, "mount sin auth"))
            continue
        if path in allowed:
            continue
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            # Route de Starlette (docs/openapi): fuera del árbol de dependencias
            # donde vive la auth ⇒ pública.
            offenders.append((path, f"{type(route).__name__} sin árbol de dependencias"))
            continue
        names = _auth_dep_names(dependant)
        names |= {getattr(c, "__qualname__", None) or type(c).__name__ for c in heredadas}
        if not any(marker in n for n in names for marker in _AUTH_MARKERS):
            offenders.append((path, f"sin dep de auth (deps: {sorted(names)})"))
    return offenders


@pytest.mark.noauth
def test_toda_ruta_de_la_app_exige_autenticacion():
    from apps.web.app import app

    offenders = routes_without_auth(app)
    assert not offenders, (
        f"{len(offenders)} ruta(s) accesibles SIN login (agregá la dep de auth al "
        "router, o sumalas a la allowlist de este test a sabiendas):\n"
        + "\n".join(f"  {p}: {why}" for p, why in offenders)
    )


@pytest.mark.noauth
def test_el_walker_detecta_un_router_sin_dependencies():
    """El chequeo tiene dientes: un router incluido sin `dependencies=` se ve."""
    from apps.web.deps_auth import get_current_user_html

    _no_docs = dict(docs_url=None, redoc_url=None, openapi_url=None)
    unprotected = FastAPI(**_no_docs)

    @unprotected.get("/secreto")
    def _secreto():
        return {}

    assert [p for p, _ in routes_without_auth(unprotected)] == ["/secreto"]

    protected = FastAPI(**_no_docs)

    @protected.get("/secreto", dependencies=[Depends(get_current_user_html)])
    def _secreto_ok():
        return {}

    assert routes_without_auth(protected) == []


@pytest.mark.noauth
def test_el_walker_no_saltea_rutas_sin_arbol_de_dependencias():
    """Las docs de OpenAPI son `starlette.routing.Route` (sin `.dependant`): el
    walker las cuenta como violación en vez de ignorarlas en silencio."""
    with_docs = FastAPI(docs_url="/docs", openapi_url="/openapi.json")
    offenders = dict(routes_without_auth(with_docs))
    assert "/openapi.json" in offenders and "/docs" in offenders


@pytest.mark.noauth
def test_el_walker_ve_TODAS_las_rutas_y_no_solo_las_de_nivel_app():
    """El guard de arriba sólo sirve si efectivamente RECORRE la app.

    Esta es la lección de 2026-09-04: con FastAPI 0.141 (la versión que instala
    `requirements.txt`, o sea la que corre el servidor) `app.routes` dejó de traer las
    rutas de los routers incluidos y pasó a traer un wrapper sin `.path`. El walker
    veía 4 rutas en vez de 66. Esta vez falló ruidoso por casualidad —los wrappers
    cayeron en la rama "sin árbol de dependencias"—; con otra forma podría haber
    quedado VERDE inspeccionando nada, que es el peor final posible para un guard de
    seguridad. Este test le pone piso: si mañana el recorrido vuelve a quedar ciego,
    rompe acá y no en producción.
    """
    from apps.web.app import app

    from tests._routes import app_route_paths

    paths = app_route_paths(app)
    assert len(paths) >= 40, (
        f"el walker sólo ve {len(paths)} rutas: casi seguro FastAPI volvió a cambiar la "
        f"forma de `app.routes` y `tests/_routes.py` quedó ciego. Rutas: {sorted(paths)}")
    # Una de cada familia: router público, router con RequireTabPermission, router de
    # admin, parcial de HTMX y ruta declarada directo en la app.
    for esperada in ("/login", "/panels/{panel_id}/rows", "/users", "/stream", "/api/health"):
        assert esperada in paths, f"falta {esperada} — el recorrido está incompleto"
