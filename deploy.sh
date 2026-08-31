#!/bin/bash
# Script de actualización automática para el servidor de producción.
# Para ejecutarlo: bash deploy.sh

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

echo ">>> Despliegue completado."
echo "Para ver los logs: journalctl -u monitores.service -f"
