"""Cierre Z3 (ítems 3 y 4) — el `truststore` muerto que sobrevivió en el script de seed,
y el comentario de `requests` que todavía lo citaba como justificación del pin.

Contexto: la ronda anterior BORRÓ `import truststore; truststore.inject_into_ssl()` de
`core/infrastructure/byma/catalog_enrich.py` (el camino de PRODUCCIÓN de la ficha
técnica BYMA) y lo reemplazó por la política TLS única del repo
(`core/infrastructure/_tls.should_verify`), porque `truststore` no está declarado en
ningún requirements ni instalado: el `except ImportError: pass` sólo disimulaba código
muerto — parecía que el módulo resolvía su TLS y no hacía nada.

Quedaron dos residuos:

  · `scripts/byma_enrich_seed_isin.py` conservaba el mismo bloque, con un comentario que
    decía «mismo patrón que catalog_enrich.py» — ya falso. Peor: su `requests.Session()`
    quedaba con el default de `requests` y por lo tanto FUERA de
    `MONITOR_TLS_NO_VERIFY_HOSTS`, la única perilla operativa del repo para una cadena
    rota (el docstring de `_tls.py` es explícito: «no volver a hardcodear un verify en un
    cliente, que deja este override inerte»).

  · `requirements.lock` y `requirements.txt` justificaban el pin de `requests` como
    «Session + truststore». La mitad de la ficha técnica de la dependencia era falsa.

Estos tests atan las dos puntas: el script no vuelve a importar truststore, su Session
sigue la MISMA política que la de producción, y los requirements no vuelven a nombrar
truststore como motivo de un pin.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

import core.infrastructure.byma.catalog_enrich as ce

pytestmark = pytest.mark.noauth

RAIZ = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "scripts" / "byma_enrich_seed_isin.py"
REQUIREMENTS = ("requirements.txt", "requirements.lock", "requirements-dev.txt")


def _arbol() -> ast.Module:
    return ast.parse(SCRIPT.read_text(encoding="utf-8"))


def _modulos_importados(tree: ast.Module) -> set[str]:
    return {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in getattr(node, "names", [])
    }


# ------------------------------------------------- truststore: enterrado también acá --

def test_el_script_no_importa_truststore():
    """Mismo guard que `test_rem_R2_infra_catalog_enrich`, sobre el script gemelo."""
    tree = _arbol()
    assert "truststore" not in _modulos_importados(tree), (
        "scripts/byma_enrich_seed_isin.py volvió a importar truststore: no está "
        "declarado en ningún requirements, así que es código muerto que el "
        "`except ImportError` disimula")
    llamadas = [n.func.attr for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert "inject_into_ssl" not in llamadas


def test_ningun_requirements_nombra_truststore():
    """Ítem 4: el comentario del pin de `requests` lo citaba como justificación
    («Session + truststore») cuando ya no existía en el código. El pin sigue
    justificado —`requests.Session` es la ficha BYMA— pero por el otro motivo."""
    menciones = []
    for nombre in REQUIREMENTS:
        f = RAIZ / nombre
        if not f.is_file():
            continue
        for i, linea in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "truststore" in linea.lower():
                menciones.append(f"{nombre}:{i}: {linea.strip()}")
    assert menciones == [], (
        "un requirements vuelve a nombrar truststore. Si es un pin: revisar si conviene "
        "reponer inject_into_ssl(). Si es un comentario: está documentando algo que el "
        "código ya no hace.\n" + "\n".join(menciones))


def test_el_pin_de_requests_sigue_teniendo_dueno():
    """Ancla del test de arriba: el pin se conserva porque hay consumidores REALES.
    Si mañana no queda ninguno, el pin sobra y hay que sacarlo (no re-comentarlo)."""
    consumidores = [
        p for p in (RAIZ / "core").rglob("*.py")
        if "import requests" in p.read_text(encoding="utf-8")
    ]
    consumidores += [SCRIPT] if "import requests" in SCRIPT.read_text(encoding="utf-8") else []
    assert consumidores, (
        "nadie importa `requests` en core/ ni en el script de seed: el pin de "
        "requirements.txt/.lock quedó sin dueño")

    lock = (RAIZ / "requirements.lock").read_text(encoding="utf-8")
    assert re.search(r"^requests==", lock, re.M), (
        "requirements.lock dejó de pinear requests, pero sigue habiendo consumidores: "
        f"{[str(p.relative_to(RAIZ)) for p in consumidores]}")


# ----------------------------------------------- la Session del script usa la política --

def _script():
    return importlib.import_module("scripts.byma_enrich_seed_isin")


def test_la_session_del_script_verifica_tls_por_default(monkeypatch):
    monkeypatch.delenv("MONITOR_TLS_NO_VERIFY_HOSTS", raising=False)
    assert _script()._session().verify is True


def test_la_session_del_script_respeta_el_override_de_tls(monkeypatch):
    """Lo que el truststore muerto NO daba: una perilla que funciona de verdad."""
    monkeypatch.setenv("MONITOR_TLS_NO_VERIFY_HOSTS", "open.bymadata.com.ar")
    assert _script()._session().verify is False


def test_la_session_del_script_y_la_de_produccion_deciden_igual(monkeypatch):
    """«Mismo patrón que catalog_enrich.py» ahora es CIERTO (era el comentario que
    mentía): las dos sesiones de la ficha BYMA responden a la misma perilla."""
    monkeypatch.delenv("MONITOR_TLS_NO_VERIFY_HOSTS", raising=False)
    assert _script()._session().verify is ce._ficha_session().verify is True

    monkeypatch.setenv("MONITOR_TLS_NO_VERIFY_HOSTS", "open.bymadata.com.ar")
    assert _script()._session().verify is ce._ficha_session().verify is False


def test_la_session_del_script_lleva_los_headers_de_la_ficha():
    """El helper no puede perder el `Token`/`Options` de la ficha técnica: sin eso el
    script corre pero la API devuelve vacío y el seed "no encuentra" ningún ISIN."""
    s = _script()._session()
    assert s.headers.get("Token") and s.headers.get("Options") == "technical-details"


def test_main_usa_el_helper_y_no_arma_su_propia_session():
    """Sin esto, `_session()` puede quedar decorativo: `main()` vuelve a
    `requests.Session()` inline y la política TLS deja de aplicarse donde importa."""
    tree = _arbol()
    main = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    llamadas = [n.func for n in ast.walk(main) if isinstance(n, ast.Call)]
    por_nombre = {f.id for f in llamadas if isinstance(f, ast.Name)}
    por_atributo = {f"{getattr(f.value, 'id', '?')}.{f.attr}"
                    for f in llamadas if isinstance(f, ast.Attribute)}

    assert "_session" in por_nombre, "main() dejó de usar el helper `_session()`"
    assert "requests.Session" not in por_atributo, (
        "main() volvió a armar su propia `requests.Session()`: esa queda con el default "
        "de requests, fuera de MONITOR_TLS_NO_VERIFY_HOSTS")
