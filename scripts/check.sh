#!/bin/bash
# Gate de calidad en Linux: ruff + pytest. Falla (exit 1) si cualquiera falla.
#
# Gemelo de `scripts/check.ps1` (Windows). Lo corren DOS consumidores:
#   * el CI de GitHub Actions (.github/workflows/gate.yml), en x86 y en ARM;
#   * un humano en el servidor, para verificar en la misma arquitectura que produce.
#
# Es el "¿está verde el repo?" canónico antes de pushear a
# origin/main (github.com/IronCondorBursatil/MonitorMercadoArgy): el deploy sale de
# ahí. Equivale a un CI gate — y desde que existe gate.yml, ES el CI gate.
#
# Uso:
#   bash scripts/check.sh                 # ruff + pytest completo
#   bash scripts/check.sh --fast          # pytest -x (corta en el 1er fallo)
#   bash scripts/check.sh --install-dev   # instala requirements-dev.txt primero
#
# `--install-dev` es OPT-IN a propósito: `deploy.sh` instala sólo requirements.txt
# (pytest/hypothesis/ruff no van al servidor). En el server, correr el gate con
# --install-dev mete esas deps en el venv de producción; es aceptable (hay 193 GB
# de disco; los 47 MB eran un argumento de la era Render/512 MB) pero tiene que ser
# una decisión explícita, no un efecto colateral de un deploy.

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

FAST=0
INSTALL_DEV=0
for arg in "$@"; do
    case "$arg" in
        --fast) FAST=1 ;;
        --install-dev) INSTALL_DEV=1 ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "argumento desconocido: $arg" >&2; exit 2 ;;
    esac
done

# Intérprete: el venv del repo si está (el del servidor), si no python3.12, si no
# python3 SOLO si ya es 3.12. Misma regla que `_pick_python312` de deploy.sh —
# run.py aborta con SystemExit en cualquier otra minor.
_es_312() { "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' 2>/dev/null; }

PY=""
for cand in "venv/bin/python" "python3.12" "python3"; do
    if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then
        if _es_312 "$cand"; then PY="$cand"; break; fi
    fi
done
if [ -z "$PY" ]; then
    echo "!!! No encontré Python 3.12 (probé venv/bin/python, python3.12, python3)." >&2
    echo "    El proyecto lo EXIGE (run.py aborta con otra minor):" >&2
    echo "      sudo apt install python3.12 python3.12-venv" >&2
    exit 1
fi
echo "==> intérprete: $PY ($("$PY" -V 2>&1))"

if [ "$INSTALL_DEV" = "1" ]; then
    echo "==> instalando requirements-dev.txt"
    "$PY" -m pip install -q -r requirements-dev.txt || exit 1
fi

FAILED=0

echo "==> ruff check"
"$PY" -m ruff check . || { FAILED=1; echo "ruff FALLÓ"; }

echo
echo "==> pytest"
if [ "$FAST" = "1" ]; then
    "$PY" -m pytest tests/ -q -x || { FAILED=1; echo "pytest FALLÓ"; }
else
    "$PY" -m pytest tests/ -q || { FAILED=1; echo "pytest FALLÓ"; }
fi

if [ "$FAILED" = "1" ]; then
    echo
    echo "=== GATE ROJO ==="
    exit 1
fi
echo
echo "=== GATE VERDE ==="
exit 0
