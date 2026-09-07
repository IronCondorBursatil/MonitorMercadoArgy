"""relock.py — regenera requirements.lock desde un freeze VALIDADO (el que publica el CI).

    py -3.12 scripts/relock.py <freeze.txt> [--check] [--source "CI run 123, 2026-09-08"]

El lock de este repo es una lista CURADA a mano (secciones, comentarios, pins de las
transitivas que afectan runtime), no un `pip freeze`. Este script la actualiza EN SU LUGAR:
cada `paquete==versión` toma la versión del freeze y conserva todo lo que tenga a la
derecha (comentario, marker). Contrato completo en tests/test_relock.py y en
agents.md §0.3 (regla 2).

Reglas duras:
  · un paquete pineado en el lock o declarado en requirements.txt que NO esté en el freeze
    es error: el freeze no viene de un entorno que instaló requirements.txt;
  · una versión del freeze que viole una cota del .txt es error: ese CI no debería estar
    verde, y el lock no puede contradecir al .txt (tests/test_aud_F_ops_deploy_lock.py);
  · `uvloop` (event loop de prod; no hay wheel para Windows) entra con el marker
    `; sys_platform != 'win32'` — pip lo saltea en la laptop y lo instala en Linux;
  · un paquete nuevo del .txt sin línea en el lock se agrega al final, en una sección
    propia, para ubicarlo a mano en la sección que corresponda;
  · `--check` no escribe: sale 1 si hay cambios, 0 si el lock está al día, 2 ante error.

El freeze tiene que ser de LINUX (artifact `freeze-ubuntu-latest` de gate.yml o
deps-refresh.yml). Un `pip freeze` de Windows no trae uvloop y sí colorama/pywinpty.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent

# `  uvicorn[standard]==0.48.0   # comentario` → indent, nombre, extras, `==`, versión, resto
_PIN = re.compile(r"^(\s*)([A-Za-z0-9_.\-]+)(\[[^\]]+\])?(\s*==\s*)([^\s;#]+)(.*)$")
# líneas de `pip freeze`: `nombre==versión` (ignora `pkg @ file://…` y `-e …`)
_FREEZE = re.compile(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]+\])?==([^\s;#]+)")

# Paquetes condicionales por plataforma que SÍ afectan el runtime servido y por eso van al
# lock con marker. colorama/pywinpty/pexpect/ptyprocess se dejan afuera a propósito: no
# tocan el runtime (consola / Jupyter que arrastra optionlab).
PLATFORM_MARKERS = {"uvloop": "sys_platform != 'win32'"}
_INSERT_BEFORE = {"uvloop": "httpx=="}   # queda junto al bloque de uvicorn[standard]
_MARKER_COMMENT = {"uvloop": "event loop de prod; el marker lo saltea en Windows (sin wheel)"}


class RelockError(Exception):
    """El freeze no sirve para regenerar este lock (ver mensaje)."""


def parse_freeze(text: str) -> dict[str, str]:
    """`nombre-normalizado → versión` de un `pip freeze`."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _FREEZE.match(line)
        if m:
            out[canonicalize_name(m.group(1))] = m.group(2)
    return out


def parse_requirements(text: str) -> list[Requirement]:
    """Las líneas de requirements.txt como `packaging.requirements.Requirement`."""
    reqs: list[Requirement] = []
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        reqs.append(Requirement(line))
    return reqs


def _validar_cotas(freeze: dict[str, str], reqs: list[Requirement]) -> None:
    for req in reqs:
        version = freeze.get(canonicalize_name(req.name))
        if version is None:
            raise RelockError(f"{req.name}: declarado en requirements.txt pero ausente en el freeze")
        if req.specifier and not req.specifier.contains(version, prereleases=True):
            raise RelockError(
                f"{req.name}: el freeze trae {version}, que no satisface `{req.specifier}` de "
                "requirements.txt — ese entorno no instaló este requirements.txt"
            )


def relock(lock_text: str, freeze: dict[str, str], reqs: list[Requirement], *,
           source_label: str) -> tuple[str, list[str]]:
    """Devuelve (lock nuevo, lista de cambios 'paquete viejo -> nuevo')."""
    _validar_cotas(freeze, reqs)

    out: list[str] = []
    changes: list[str] = []
    seen: set[str] = set()
    for i, line in enumerate(lock_text.splitlines()):
        if i == 0 and line.startswith("# Lockfile"):
            out.append(f"# Lockfile — versiones EXACTAS validadas por el CI ({source_label}, Python 3.12).")
            continue
        m = _PIN.match(line)
        if not m:
            out.append(line)
            continue
        indent, name, extras, eq, old, rest = m.groups()
        key = canonicalize_name(name)
        seen.add(key)
        new = freeze.get(key)
        if new is None:
            raise RelockError(
                f"{name}: pineado en requirements.lock pero ausente en el freeze "
                "(¿el freeze no instaló requirements.txt, o el paquete dejó de ser transitiva?)"
            )
        if new != old:
            changes.append(f"{name} {old} -> {new}")
        out.append(f"{indent}{name}{extras or ''}{eq}{new}{rest}")

    for pkg, marker in PLATFORM_MARKERS.items():
        key = canonicalize_name(pkg)
        if key in seen or key not in freeze:
            continue
        line = f"{pkg}=={freeze[key]} ; {marker}  # {_MARKER_COMMENT[pkg]}"
        anchor = _INSERT_BEFORE.get(pkg)
        idx = next((j for j, ln in enumerate(out) if anchor and ln.startswith(anchor)), None)
        if idx is None:
            out.append(line)
        else:
            out.insert(idx, line)
        seen.add(key)
        changes.append(f"{pkg} (nuevo) -> {freeze[key]}")

    nuevos = [r for r in reqs if canonicalize_name(r.name) not in seen]
    if nuevos:
        out.append("")
        out.append("# --- nuevos (agregados por scripts/relock.py; ubicar en su sección a mano) ---")
        for r in nuevos:
            key = canonicalize_name(r.name)
            extras = f"[{','.join(sorted(r.extras))}]" if r.extras else ""
            out.append(f"{r.name}{extras}=={freeze[key]}")
            seen.add(key)
            changes.append(f"{r.name} (nuevo) -> {freeze[key]}")

    return "\n".join(out) + "\n", changes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("freeze", help="pip freeze de LINUX validado por el CI")
    ap.add_argument("--lock", default=str(ROOT / "requirements.lock"))
    ap.add_argument("--requirements", default=str(ROOT / "requirements.txt"))
    ap.add_argument("--check", action="store_true", help="no escribe; exit 1 si hay cambios")
    ap.add_argument("--source", default=None, help="etiqueta para la cabecera (run del CI, fecha)")
    args = ap.parse_args(argv)

    freeze_path = Path(args.freeze)
    lock_path = Path(args.lock)
    try:
        freeze = parse_freeze(freeze_path.read_text(encoding="utf-8"))
        reqs = parse_requirements(Path(args.requirements).read_text(encoding="utf-8"))
        nuevo, changes = relock(lock_path.read_text(encoding="utf-8"), freeze, reqs,
                                source_label=args.source or freeze_path.name)
    except (RelockError, OSError) as exc:
        print(f"relock: ERROR: {exc}", file=sys.stderr)
        return 2

    if not changes:
        print(f"relock: {lock_path.name} al día con {freeze_path.name} ({len(freeze)} paquetes en el freeze)")
        return 0
    print(f"relock: {len(changes)} cambio(s) respecto de {freeze_path.name}:")
    for c in changes:
        print(f"  {c}")
    if args.check:
        return 1
    lock_path.write_text(nuevo, encoding="utf-8")
    print(f"relock: escrito {lock_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
