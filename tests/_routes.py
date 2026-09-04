"""Recorrido de las rutas de la app que funciona en CUALQUIER versión de FastAPI.

Por qué existe: hasta FastAPI 0.140, `app.include_router(r, dependencies=[dep])`
APLANABA el router — cada `APIRoute` quedaba directamente en `app.routes`, con su
`.path` y con `dep` ya horneada en su `.dependant`. Un test podía recorrer
`app.routes` mirando `route.path` y `route.dependant` y ver todo.

Desde **0.141** (la versión que instala `requirements.txt`, o sea **la que corre el
servidor**) eso cambió: `app.routes` guarda un wrapper `_IncludedRouter` que NO tiene
`.path` ni `.dependant`, las rutas reales cuelgan de `wrapper.original_router.routes`,
y las dependencias del `include_router(...)` viven en
`wrapper.include_context.dependencies` — **no** en la ruta.

Consecuencia medida (2026-09-04, Oracle/aarch64, fastapi 0.141.1 + starlette 1.6.0):
`app.routes` pasó de 66 rutas con `.path` a 21 objetos de los cuales sólo 4 lo tienen.
Los tres tests que recorrían `app.routes` se rompieron — incluido el guard de auditoría
`test_toda_ruta_de_la_app_exige_autenticacion`, que es un GUARD DE SEGURIDAD. Y el
arreglo ingenuo (recorrer `original_router.routes` y mirar sólo `route.dependant`)
reportaría todas las rutas de paneles como SIN AUTH: un falso positivo por cada ruta,
porque la dependencia está en el contexto del include.

Verificado en vivo el mismo día: en producción la auth SÍ aplica (toda ruta protegida
devuelve 302 a `/login` sin cookie). Lo que estaba roto era la capacidad del test de
verificarlo, no la app.
"""

from __future__ import annotations

from typing import Iterator, Tuple


def iter_app_routes(app) -> Iterator[Tuple[object, tuple]]:
    """(ruta, dependencias_heredadas_del_include) por cada ruta REAL de `app`.

    `dependencias_heredadas` son los callables que se pasaron en
    `include_router(..., dependencies=[Depends(x)])`: en FastAPI ≥ 0.141 no están en
    el `dependant` de la ruta y hay que sumarlos aparte. En las versiones viejas la
    lista viene vacía porque ya venían horneadas.
    """
    for route in app.routes:
        inner = getattr(route, "original_router", None)
        if inner is None:                      # FastAPI ≤ 0.140, Mount, o @app.get
            yield route, ()
            continue
        ctx = getattr(route, "include_context", None)
        heredadas = tuple(
            getattr(d, "dependency", d)
            for d in (getattr(ctx, "dependencies", None) or ())
        )
        for sub in (getattr(inner, "routes", None) or ()):
            yield sub, heredadas


def app_route_paths(app) -> set:
    """Paths de todas las rutas de `app` (sin los `None` de los wrappers)."""
    return {p for p in (getattr(r, "path", None) for r, _ in iter_app_routes(app)) if p}
