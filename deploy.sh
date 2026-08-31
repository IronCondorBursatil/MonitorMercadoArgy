#!/bin/bash
# Script de actualización automática para el servidor de producción.
# Para ejecutarlo: bash deploy.sh
set -euo pipefail   # aborta si un paso falla (antes imprimía "completado" igual)

echo "======================================"
echo "Iniciando despliegue de Monitor Renta Fija"
echo "======================================"

# 1. Traer los últimos cambios de GitHub
echo ">>> Descargando últimas actualizaciones..."
git pull origin main || git pull origin master

# 2. Instalar nuevas dependencias si las hay
echo ">>> Instalando dependencias de Python..."
source venv/bin/activate
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
