"""Remediación R2 (residuo del hallazgo A-7): el comentario de
`core/infrastructure/byma/credentials.py::save_credentials` afirmaba que su
`ValueError` "el router lo traduce a 400", y era FALSO.

`apps/web/routers/source.py::source_credentials` llama a `save_credentials(...)`
FUERA de todo try/except —el único `try` del handler envuelve el probe OAuth
`_ensure_token`— y `apps/web/app.py` no registra `@app.exception_handler(ValueError)`
(solo RequiresLogin / TabForbidden). O sea que ese camino devuelve **500**, no 400.

Severidad baja pero NO es inalcanzable: `_FORBIDDEN` no es la única condición —
también se rechaza `=` en el usuario, y un usuario BYMA con `=` en el nombre pasa
el probe OAuth (login válido) y recién muere en `save_credentials`.

Este archivo ata las DOS PUNTAS para que el comentario no vuelva a mentir. El
módulo declara el status real en una marca greppeable (`STATUS-HTTP-REAL: <cód>`) y
el test la compara contra lo que el router hace SEGÚN SU AST. Se pone rojo en los
dos sentidos: si alguien reintroduce la promesa del 400 sin arreglar el router, y
también el día que alguien agregue el `try/except ValueError` en el router (el fix
completo, archivo de otro lote) y se olvide de actualizar el comentario a 400.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from apps.web.routers import source as source_router
from core.infrastructure.byma import credentials as cred


def _router_tree() -> ast.Module:
    return ast.parse(Path(source_router.__file__).read_text(encoding="utf-8"))


def _es_llamada_a_save_credentials(n) -> bool:
    """True para `save_credentials(...)` Y para `credentials.save_credentials(...)`.

    Matchear solo `ast.Name` dejaba un agujero en el guard: si el router pasaba a
    llamarla calificada por módulo, el detector devolvía False en silencio (=> "500")
    aunque la llamada estuviera envuelta en try/except."""
    if not isinstance(n, ast.Call):
        return False
    f = n.func
    return ((isinstance(f, ast.Name) and f.id == "save_credentials")
            or (isinstance(f, ast.Attribute) and f.attr == "save_credentials"))


def _llamadas_a_save_credentials(tree) -> list:
    return [n for n in ast.walk(tree) if _es_llamada_a_save_credentials(n)]


def _captura_valueerror(tree) -> bool:
    """True si el árbol envuelve una llamada a `save_credentials` en un try/except
    que capture ValueError (o Exception)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        # solo el CUERPO del try (no los handlers ni el else/finally)
        llama = any(_es_llamada_a_save_credentials(n)
                    for stmt in node.body for n in ast.walk(stmt))
        if not llama:
            continue
        for h in node.handlers:
            if h.type is None:
                return True
            nombres = [t.id for t in ast.walk(h.type) if isinstance(t, ast.Name)]
            if {"ValueError", "Exception", "BaseException"} & set(nombres):
                return True
    return False


def _router_captura_valueerror_de_save_credentials() -> bool:
    return _captura_valueerror(_router_tree())


def _status_documentado() -> int:
    """El número que `save_credentials` declara en su marca `STATUS-HTTP-REAL:`."""
    src = inspect.getsource(cred.save_credentials)
    m = re.search(r"STATUS-HTTP-REAL:\s*(\d{3})", src)
    assert m, ("save_credentials perdió la marca `STATUS-HTTP-REAL: <código>`, que es "
               "lo que ata su comentario a lo que el router hace de verdad.")
    return int(m.group(1))


def test_el_status_documentado_coincide_con_lo_que_hace_el_router():
    """Guard bidireccional (el ítem de la auditoría: "que código y comentario
    coincidan"). Reality check por AST, no por fe:

      · router SIN try/except alrededor de `save_credentials`  → 500
      · router CON try/except que capture ValueError           → 400

    Si alguien reintroduce la promesa vieja del 400 sin arreglar el router, o
    arregla el router y deja el 500 escrito, esto se pone rojo."""
    real = 400 if _router_captura_valueerror_de_save_credentials() else 500
    documentado = _status_documentado()
    assert documentado == real, (
        f"save_credentials documenta STATUS-HTTP-REAL: {documentado} pero el router "
        f"devuelve {real}. "
        + ("El router YA captura el ValueError: actualizá el comentario a 400."
           if real == 400 else
           "apps/web/routers/source.py llama a save_credentials fuera de try/except, "
           "así que el ValueError sale 500 (no hay exception_handler(ValueError) en "
           "apps/web/app.py). Arreglá el router o el comentario.")
    )


def test_el_router_sigue_llamando_a_save_credentials():
    """Ancla del guard: si la llamada desaparece del router, el test de arriba se
    volvería vacuo (0 nodos Try candidatos ⇒ "no captura") sin avisar.

    Se exige un nodo Call de verdad, no la subcadena: un `save_credentials` que
    sobreviva solo en un import o en un comentario dejaría el guard vacuo igual."""
    assert _llamadas_a_save_credentials(_router_tree()), (
        "apps/web/routers/source.py ya no LLAMA a save_credentials: el guard de "
        "STATUS-HTTP-REAL quedó vacuo, hay que revisar el contrato del módulo.")


def test_el_contrato_del_modulo_es_propagar_valueerror(tmp_path):
    """Lo único que `save_credentials` garantiza: levanta ValueError y NO escribe."""
    envp = tmp_path / ".env"
    envp.write_text("OTRA=1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        cred.save_credentials("u=X", "pass", path=envp)
    assert envp.read_text(encoding="utf-8") == "OTRA=1\n"


# ------------------------------------------------- el guard se guarda a sí mismo --

_SIN_TRY = """
def h():
    save_credentials(u, p)
"""

_CON_TRY_NOMBRE = """
def h():
    try:
        save_credentials(u, p)
    except ValueError as e:
        return _err(str(e))
"""

_CON_TRY_CALIFICADA = """
def h():
    try:
        credentials.save_credentials(u, p)
    except ValueError as e:
        return _err(str(e))
"""

_TRY_QUE_NO_LA_ENVUELVE = """
def h():
    try:
        await probe._ensure_token()
    except Exception as e:
        return _err(str(e))
    save_credentials(u, p)
"""


@pytest.mark.parametrize("fuente, espera", [
    pytest.param(_SIN_TRY, False, id="sin-try"),
    pytest.param(_CON_TRY_NOMBRE, True, id="try-llamada-por-nombre"),
    pytest.param(_CON_TRY_CALIFICADA, True, id="try-llamada-calificada"),
    pytest.param(_TRY_QUE_NO_LA_ENVUELVE, False, id="try-que-envuelve-otra-cosa"),
])
def test_el_detector_ast_distingue_los_cuatro_casos(fuente, espera):
    """El guard de arriba vale lo que valga su detector. Sin esto, un detector que
    devolviera siempre False (p.ej. por matchear solo `ast.Name` mientras el router
    llama `credentials.save_credentials(...)`) pasaría inadvertido: coincidiría con
    el 500 documentado hoy y el test seguiría verde el día que alguien SÍ arregle el
    router. El caso `try-que-envuelve-otra-cosa` es la forma EXACTA que tiene hoy
    apps/web/routers/source.py (el try envuelve el probe OAuth, no el save)."""
    assert _captura_valueerror(ast.parse(fuente)) is espera
