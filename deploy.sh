#!/bin/bash
# Script de actualización automática para el servidor de producción.
# Para ejecutarlo: bash deploy.sh
set -euo pipefail   # aborta si un paso falla (antes imprimía "completado" igual)

# Todo lo de abajo es RELATIVO al repo (venv/, requirements.txt, el healthcheck):
# correrlo desde otro cwd tomaba —o creaba— el venv equivocado.
cd "$(dirname "$0")"

echo "======================================"
echo "Iniciando despliegue de Monitor Renta Fija"
echo "======================================"

# 1. Traer los últimos cambios de GitHub
echo ">>> Descargando últimas actualizaciones..."
git pull origin main || git pull origin master

# 2. venv + dependencias. IDEMPOTENTE: si el venv no está, se crea (antes el script
# SOLO lo activaba, así que un rebuild desde cero —droplet perdido, migración de
# región— frenaba acá con "venv/bin/activate: No such file or directory"). Todo esto
# corre ANTES del restart: si algo falla, `set -e` aborta y el servicio VIEJO sigue
# arriba, sin deploy a medias.
echo ">>> Preparando el entorno virtual..."

# El intérprete está PINEADO a 3.12: run.py aborta con SystemExit en cualquier otra
# minor ("Requiere Python 3.12.x"). Crear el venv con `python3` a secas producía —en un
# droplet donde python3 fuese 3.10/3.13— un venv que instala todo bien y recién revienta
# en el healthcheck, con el servicio caído y el error escondido en journalctl. Se valida
# ANTES: primero python3.12, y si no está se acepta python3 SOLO si ya es 3.12.
_pick_python312() {
    local cand
    for cand in python3.12 python3; do
        if command -v "$cand" >/dev/null 2>&1 \
           && "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' 2>/dev/null; then
            command -v "$cand"
            return 0
        fi
    done
    return 1
}

_venv_es_312() {
    [ -x venv/bin/python ] \
        && venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' 2>/dev/null
}

if ! _venv_es_312; then
    if [ -x venv/bin/python ]; then
        echo "!!! El venv existente NO es Python 3.12 ($(venv/bin/python -V 2>&1)):"
        echo "    run.py aborta con SystemExit y el servicio no levanta."
        echo "    Borralo y volvé a correr el deploy:  rm -rf venv && bash deploy.sh"
        exit 1
    fi
    PY312="$(_pick_python312 || true)"
    if [ -z "${PY312:-}" ]; then
        echo "!!! No encontré Python 3.12 en el PATH (probé python3.12 y python3)."
        echo "    run.py EXIGE 3.12.x: un venv de otra minor instala todo y recién falla"
        echo "    en el healthcheck. Instalalo antes de deployar:"
        echo "      sudo apt install python3.12 python3.12-venv"
        exit 1
    fi
    echo "    (no había venv: creando uno nuevo con $PY312 -m venv)"
    "$PY312" -m venv venv
    _venv_es_312 || { echo "!!! El venv recién creado no quedó en 3.12."; exit 1; }
fi
# shellcheck source=/dev/null
source venv/bin/activate

echo ">>> Instalando dependencias de Python..."
# requirements.txt (versiones abiertas) y NO requirements.lock: apuntar el deploy al
# lock quedó refutado en docs/plan-optimizacion-2026-08-31.md (:92 y :316). El lock es
# para el bootstrap local reproducible. pytest/ruff no van acá (requirements-dev.txt).
pip install -r requirements.txt

# 3. Reiniciar el servicio
echo ">>> Reiniciando el servicio systemd (monitores.service)..."
sudo systemctl restart monitores.service

# 4. Verificar que la app realmente levantó (con set -e, un fallo acá aborta y avisa).
echo ">>> Verificando /api/health..."
sleep 5
for i in $(seq 1 6); do
    if curl -fsS http://localhost:8000/api/health >/dev/null 2>&1; then
        echo ">>> Health OK. Despliegue completado."
        echo "Para ver los logs: journalctl -u monitores.service -f"
        exit 0
    fi
    echo "    ...esperando (intento $i/6)"; sleep 5
done
echo "!!! La app NO respondió /api/health tras el restart. Revisá: journalctl -u monitores.service -n 50"
exit 1
