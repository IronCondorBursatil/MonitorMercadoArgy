"""Re-auditoría R5 — `requirements.lock` tiene que pinear las transitivas del extra.

El lock declara en su header que fija «las deps del proyecto + las transitivas que
afectan runtime». La re-auditoría del lote F_ops encontró que se había restaurado
`uvicorn[standard]==0.48.0` sin pinear **httptools**, que es exactamente la transitiva
por la que existe el extra: `pip install -r requirements.lock` la resolvía a lo que
hubiera ese día, o sea que el parser HTTP del hot-path quedaba fuera del lock.

Este test no mira un nombre hardcodeado: lee la metadata REAL del paquete instalado,
resuelve qué exige el extra en ESTA plataforma (markers evaluados) y exige un pin
exacto en el lock para cada uno — salvo los que están documentados como deliberados.
Si mañana uvicorn agrega una dep al extra `standard`, el lock queda rojo hasta que
alguien decida qué hacer con ella.
"""

from __future__ import annotations

import importlib.metadata as md
import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement

pytestmark = pytest.mark.noauth

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements.lock"

# Deps del extra que se dejan SIN pin a propósito (el lock lo documenta al lado):
#   uvloop   → marker `sys_platform != 'win32'`: en la máquina de desarrollo (Windows,
#              sin venv) no se instala, así que no hay versión verificada que fijar.
#   colorama → sólo colorea el log de consola; no afecta el runtime servido.
_SIN_PIN_DELIBERADO = {"uvloop", "colorama"}

_PIN = re.compile(r"^\s*([A-Za-z0-9_.\-]+)(\[[^\]]+\])?\s*==\s*([^\s;#]+)", re.M)
_CON_EXTRA = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\[([^\]]+)\]\s*==", re.M)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins() -> dict[str, str]:
    out = {}
    for linea in LOCK.read_text(encoding="utf-8").splitlines():
        limpia = linea.split("#")[0]
        m = _PIN.match(limpia)
        if m:
            out[_norm(m.group(1))] = m.group(3)
    return out


def _extras_declarados() -> list[tuple[str, str]]:
    """[(paquete, extra)] tal como los declara el propio lock (p.ej. uvicorn/standard)."""
    fuera = []
    for linea in LOCK.read_text(encoding="utf-8").splitlines():
        m = _CON_EXTRA.match(linea.split("#")[0])
        if m:
            fuera.extend((m.group(1), e.strip()) for e in m.group(2).split(","))
    return fuera


def test_el_lock_pinea_las_transitivas_de_sus_extras():
    """Cada dep que el extra arrastra en ESTA plataforma tiene que estar pineada."""
    declarados = _extras_declarados()
    assert declarados, "el lock ya no declara ningún extra (¿se perdió uvicorn[standard]?)"

    pins = _pins()
    faltan: dict[str, str] = {}
    incompatibles: dict[str, str] = {}
    for paquete, extra in declarados:
        try:
            requiere = md.distribution(paquete).requires or []
        except md.PackageNotFoundError:                     # pragma: no cover
            pytest.skip(f"{paquete} no está instalado: no hay metadata que consultar")
        for crudo in requiere:
            req = Requirement(crudo)
            if req.marker is None or not req.marker.evaluate({"extra": extra}):
                continue                                    # no aplica a este extra/plataforma
            nombre = _norm(req.name)
            if nombre in _SIN_PIN_DELIBERADO:
                continue
            if nombre not in pins:
                faltan[nombre] = f"{paquete}[{extra}] → {crudo}"
            elif not req.specifier.contains(pins[nombre], prereleases=True):
                # Un pin que NO satisface lo que el extra exige hace irresoluble el lock
                # entero (`pip install -r requirements.lock` → ResolutionImpossible), y
                # el bootstrap documentado en CLAUDE.md deja de existir.
                incompatibles[nombre] = (
                    f"lock=={pins[nombre]} vs {paquete}[{extra}] → {crudo}")
    assert not faltan, (
        "requirements.lock declara un extra pero NO pinea lo que ese extra arrastra: "
        "`pip install -r requirements.lock` resuelve esas versiones a lo que haya el día "
        f"del bootstrap, contra lo que promete el header del lock. Faltan: {faltan}")
    assert not incompatibles, (
        "requirements.lock pinea una transitiva en una versión que el extra NO acepta: "
        "el lock queda IRRESOLUBLE (pip aborta con ResolutionImpossible) y el bootstrap "
        f"documentado no se puede reproducir. Conflictos: {incompatibles}")


def test_httptools_queda_pineado_en_la_version_que_uvicorn_usa_de_verdad():
    """httptools es la RAZÓN de ser de `[standard]`: con él, `http='auto'` resuelve al
    parser en C; sin él uvicorn cae al h11 puro-Python. El pin tiene que coincidir con
    la versión que efectivamente está corriendo (si no, el lock documenta una ficción)."""
    pins = _pins()
    assert "httptools" in pins, (
        "requirements.lock no pinea httptools — es la transitiva por la que existe "
        "uvicorn[standard] (el parser HTTP del hot-path)")
    assert pins["httptools"] == md.version("httptools"), (
        f"el lock pinea httptools=={pins['httptools']} pero la versión verificada "
        f"funcionando es {md.version('httptools')}")


_PREFIJO_PROTOCOLOS = "uvicorn.protocols.http."


def _parser_http_cargado() -> tuple[type, str]:
    """(clase de protocolo que `Config.load()` eligió, paquete que la aporta).

    uvicorn nombra sus implementaciones `uvicorn.protocols.http.<paquete>_impl`
    (`httptools_impl` / `h11_impl`), y `<paquete>` es la distribución que ese módulo
    importa. El nombre se DERIVA del runtime en vez de hardcodearse: así el test sigue
    al parser que uvicorn resuelve de verdad, y si mañana `[standard]` cambia de parser
    el lock tiene que pinear el nuevo, no el que alguien escribió acá."""
    from uvicorn.config import Config

    async def _app(scope, receive, send):      # ASGI mínimo: acá sólo importa el parser
        pass

    cfg = Config(_app)
    cfg.load()
    clase = cfg.http_protocol_class
    modulo = clase.__module__
    assert modulo.startswith(_PREFIJO_PROTOCOLOS), (
        f"uvicorn cambió dónde viven sus protocolos HTTP ({modulo}): la derivación del "
        "paquete de este test quedó obsoleta, hay que revisarla")
    return clase, _norm(modulo.rsplit(".", 1)[-1].removesuffix("_impl"))


def test_el_parser_http_que_uvicorn_carga_de_verdad_esta_pineado_en_el_lock():
    """El extra tiene que servir para algo Y estar cubierto por el lock.

    La versión anterior de este test sólo miraba `cfg.http_protocol_class.__module__`,
    o sea el ENTORNO instalado — nunca el lock. Era decorativo: comentando el pin de
    `httptools` en `requirements.lock` seguía verde, que es exactamente la regresión que
    este archivo existe para atrapar (un bootstrap `pip install -r requirements.lock` que
    resuelve el parser del hot-path a lo que haya ese día). Ahora ata las dos puntas:
    el paquete que uvicorn carga EN VIVO tiene que estar pineado en el lock, y en la
    misma versión que está corriendo."""
    clase, paquete = _parser_http_cargado()

    assert paquete != "h11", (
        f"uvicorn cayó al parser puro-Python ({clase}): el extra [standard] no está "
        "surtiendo efecto en este entorno, así que pinear sus transitivas sería cargo cult")

    try:
        instalada = md.version(paquete)
    except md.PackageNotFoundError:            # pragma: no cover — derivación rota
        pytest.fail(f"el parser cargado ({clase}) no resuelve a una distribución "
                    f"instalada ({paquete!r}): revisar la derivación del test")

    pins = _pins()
    assert paquete in pins, (
        f"uvicorn está corriendo con el parser de `{paquete}` (por el extra [standard]) "
        f"pero requirements.lock NO lo pinea: `pip install -r requirements.lock` deja el "
        "parser HTTP del hot-path a la versión que haya el día del bootstrap")
    assert pins[paquete] == instalada, (
        f"el lock pinea {paquete}=={pins[paquete]} pero la versión verificada corriendo "
        f"es {instalada}: el lock documenta una ficción")
