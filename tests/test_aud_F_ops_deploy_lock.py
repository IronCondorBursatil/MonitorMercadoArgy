"""Auditoría F_ops — deploy.sh, requirements.lock, CSS muerto y scratch/ gitignoreado.

  · `deploy.sh` hacía `source venv/bin/activate` y NUNCA creaba el venv: con
    `set -euo pipefail`, un rebuild desde cero (droplet perdido, migración de región)
    frena ahí (`bash: venv/bin/activate: No such file or directory`). Cuatro documentos
    del repo afirman que el script lo crea.
  · `requirements.lock` pinea `uvicorn==0.48.0` SIN el extra `[standard]` que declara
    `requirements.txt` — única discrepancia entre los dos archivos. El extra arrastra
    `httptools`, así que una máquina limpia que sigue el bootstrap documentado corre
    uvicorn sobre el parser h11 puro-Python y el droplet sobre httptools.
  · `apps/web/dark-financial-dashboard.css` estaba versionado, no lo referenciaba
    nadie y ni siquiera es servible: el único mount de estáticos es `apps/web/static`.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# deploy.sh
# --------------------------------------------------------------------------- #
def _deploy() -> str:
    return (ROOT / "deploy.sh").read_text(encoding="utf-8")


def test_deploy_crea_el_venv_antes_de_activarlo():
    """Idempotente: si ya está lo reusa; si no, lo crea. Sin esto un rebuild frena.

    El intérprete concreto lo pinea (y lo ejercita corriendo el bloque) el módulo
    tests/test_rem_R5_ops_tests_deploy_venv.py: acá sólo importa el ORDEN."""
    txt = _deploy()
    activa = re.search(r"^\s*source venv/bin/activate", txt, re.M)
    crea = re.search(r"^\s*\S+ -m venv venv", txt, re.M)
    assert activa, "deploy.sh ya no activa el venv"
    assert crea, "deploy.sh no crea el venv en ningún lado"
    assert crea.start() < activa.start(), "el venv se crea DESPUÉS de activarlo"


def test_deploy_no_reinicia_el_servicio_si_falla_antes():
    """Fail-safe: con `set -e`, un fallo de pull/venv/pip aborta ANTES del restart y
    el servicio viejo sigue arriba (no queda un deploy a medias)."""
    txt = _deploy()
    assert re.search(r"^set -euo pipefail", txt, re.M)
    assert txt.index("pip install") < txt.index("systemctl restart")


def test_deploy_trabaja_sobre_su_propio_directorio():
    """`venv/`, `requirements.txt` y el healthcheck son relativos: correr el script
    desde otro cwd tomaba —o creaba— el venv equivocado."""
    assert re.search(r"cd \"?\$\(dirname", _deploy())


# --------------------------------------------------------------------------- #
# requirements.lock ↔ requirements.txt
# --------------------------------------------------------------------------- #
_REQ = re.compile(r"^\s*([A-Za-z0-9_.\-]+)(\[[^\]]+\])?", re.M)


def _reqs(name: str) -> dict[str, str]:
    out = {}
    for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        m = _REQ.match(line)
        if m:
            out[m.group(1).lower()] = m.group(2) or ""
    return out


def test_el_lock_declara_los_mismos_extras_que_requirements():
    """Un extra que falta en el lock cambia el runtime (uvicorn[standard] → httptools)
    sin que nada avise: el bootstrap documentado deja de reproducir producción."""
    txt, lock = _reqs("requirements.txt"), _reqs("requirements.lock")
    faltan = {p: e for p, e in txt.items() if e and lock.get(p, "") != e}
    assert not faltan, f"extras perdidos en requirements.lock: {faltan}"


# Paridad de PINS (2026-09, Fase 1 de agents.md §0.8). El test de extras de arriba saltea
# todo paquete sin extras, así que una dep agregada solo al .txt —o una cota nueva que el
# lock viejo ya no satisface (fastapi>=0.141 vs 0.136.3)— pasaba el gate sin que nada avise.
_PIN = re.compile(r"^\s*([A-Za-z0-9_.\-]+)(\[[^\]]+\])?\s*==\s*([^\s;#]+)")


def _pins(name: str) -> dict[str, str]:
    """`paquete-normalizado → versión` de cada línea `x==v` (tolera extras, comentario y
    marker `; sys_platform ...` a la derecha)."""
    from packaging.utils import canonicalize_name

    out: dict[str, str] = {}
    for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
        m = _PIN.match(line.split("#")[0])
        if m:
            out[canonicalize_name(m.group(1))] = m.group(3)
    return out


def _requirements(name: str):
    from packaging.requirements import Requirement

    reqs = []
    for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            reqs.append(Requirement(line))
    return reqs


def test_todo_paquete_de_requirements_esta_pineado_en_el_lock_y_satisface_su_cota():
    """Cada paquete de requirements.txt tiene un `==` en requirements.lock y ese pin cae
    dentro del especificador del .txt. Si no: o falta el pin (dep nueva sin lockear) o el
    lock quedó viejo respecto de una cota nueva —en los dos casos la laptop deja de
    reproducir lo que instalan prod y el CI. Se regenera con scripts/relock.py."""
    from packaging.utils import canonicalize_name

    pins = _pins("requirements.lock")
    problemas = []
    for req in _requirements("requirements.txt"):
        version = pins.get(canonicalize_name(req.name))
        if version is None:
            problemas.append(f"{req.name}: sin pin `==` en requirements.lock")
        elif req.specifier and not req.specifier.contains(version, prereleases=True):
            problemas.append(f"{req.name}: lock={version} no satisface `{req.specifier}`")
    assert not problemas, "requirements.lock desfasado de requirements.txt:\n  " + "\n  ".join(problemas)


# --------------------------------------------------------------------------- #
# CSS muerto + scratch/
# --------------------------------------------------------------------------- #
def test_no_hay_css_fuera_del_static_montado():
    """`apps/web/app.py` monta UN solo static (`apps/web/static`): una hoja fuera de
    ahí no es servible por HTTP — es código muerto que confunde el mapa de la capa web."""
    sueltas = [p for p in (ROOT / "apps" / "web").rglob("*.css")
               if "static" not in p.relative_to(ROOT / "apps" / "web").parts]
    assert not sueltas, f"CSS fuera del static montado: {[str(p) for p in sueltas]}"


def test_scratch_esta_gitignoreado():
    """La raíz está sincronizada por OneDrive y `scratch/` acumula one-shots con
    credenciales en texto plano: como mínimo, que nunca llegue a git."""
    lineas = {ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert "scratch/" in lineas
