"""Cierre Z3 (ítem 6) — el `Query(...)` del `lag` era UN objeto compartido por dos
path operations.

`apps/web/routers/bonds.py` declaraba `_LAG = Query(1, ge=0, le=1, ...)` una sola vez y
lo usaba como **valor por defecto** en `detail` (`GET /bond/{t}/detail`) y en
`cer_drawer` (`GET /bond/{t}/cer`). Por ese camino —el estilo `param = _LAG`, sin
`Annotated`— FastAPI **no copia** el `FieldInfo`: `analyze_param` hace `field_info =
value` y después lo **muta** (`field_info.annotation = ...`, `field_info.in_ = ...`, el
alias). O sea que las dos rutas quedaban compartiendo un objeto mutable de estado
global; hoy las mutaciones coinciden (mismo nombre, mismo tipo) y por eso no se ve, que
es exactamente lo que lo hace peligroso: cualquier divergencia futura (un alias, un
`convert_underscores`, un tipo distinto) la escribe el último endpoint que FastAPI
analice y se la come el otro, sin error.

El arreglo es `Annotated[int, Query(...)]`, donde FastAPI **sí** copia por parámetro
(`copy_field_info`, comentado en el fuente como "Copy `field_info` because we mutate
`field_info.default` below"). Los tests miran el resultado —dos objetos distintos con
la MISMA cota— y no el estilo, así que "dos `Query()` separados" también los deja
verdes; lo que no pasa es volver a compartir uno.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.web.app import app
from apps.web.routers.bonds import router

_RUTAS_CON_LAG = ("/bond/{ticker}/detail", "/bond/{ticker}/cer")


def _field_info_del_lag(path: str):
    for route in router.routes:
        if getattr(route, "path", None) != path:
            continue
        for campo in getattr(route.dependant, "query_params", []):
            if campo.name == "lag":
                return campo.field_info
    raise AssertionError(f"{path} ya no declara un query param `lag`")


def test_cada_endpoint_tiene_su_propio_fieldinfo():
    """El invariante: NO puede haber un `FieldInfo` compartido entre path operations."""
    infos = [_field_info_del_lag(p) for p in _RUTAS_CON_LAG]

    ids = {id(fi) for fi in infos}
    assert len(ids) == len(infos), (
        "las path operations %s comparten el MISMO objeto `FieldInfo` para `lag`: "
        "FastAPI lo muta al analizar cada una (annotation/in_/alias), así que es estado "
        "mutable global entre endpoints. Usar `Annotated[int, Query(...)]` (FastAPI copia "
        "el FieldInfo por parámetro) o un objeto por endpoint." % (_RUTAS_CON_LAG,))


def test_las_dos_rutas_declaran_la_misma_cota():
    """La otra mitad: dejar de compartir el objeto no puede desincronizar la cota
    (T+0/T+1 son los ÚNICOS plazos BYMA; `settlement_byma_date` revienta con otro)."""
    cotas = []
    for path in _RUTAS_CON_LAG:
        fi = _field_info_del_lag(path)
        cotas.append((fi.default, {type(m).__name__: getattr(m, "ge", getattr(m, "le", None))
                                   for m in fi.metadata}))

    assert cotas[0] == cotas[1], f"las dos rutas divergieron en el `lag`: {cotas}"
    default, restricciones = cotas[0]
    assert default == 1, f"el default del lag dejó de ser T+1: {default}"
    assert restricciones == {"Ge": 0, "Le": 1}, (
        f"el lag dejó de estar acotado a T+0/T+1: {restricciones}")


@pytest.mark.parametrize("path", ["/bond/AL30D/detail", "/bond/AL30D/cer"])
@pytest.mark.parametrize("lag", ["5", "-1", "nan", ""])
def test_un_lag_fuera_de_rango_es_422_en_las_dos_rutas(path, lag):
    """Regresión del comportamiento que la cota compartida ya garantizaba: un lag
    inválido tiene que morir en validación (422), no adentro de
    `settlement_byma_date` (500 con traza y modal roto)."""
    with TestClient(app) as client:
        r = client.get(path, params={"lag": lag})

    assert r.status_code == 422, (
        f"{path}?lag={lag!r} devolvió {r.status_code} en vez de 422 de validación")
