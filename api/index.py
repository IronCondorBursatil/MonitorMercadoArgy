"""Punto de entrada para Vercel Serverless Functions.

Monta la aplicacion FastAPI de apps.web.app en el entorno de Vercel.
NOTA: Vercel detiene las tareas de fondo tras cada peticion, 
y el almacenamiento es volatil y de solo lectura (excepto /tmp).
"""

import sys
from pathlib import Path

# Agregar el directorio raiz al path para que pueda importar 'apps' y 'config'
root_path = Path(__file__).parent.parent
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

# Forzar la carpeta de base de datos a /tmp si estamos en Vercel
import os
os.environ["MONITOR_DB_DIR"] = "/tmp/monitor"

# Importar la app de FastAPI
from apps.web.app import app
