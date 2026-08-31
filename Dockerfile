FROM python:3.12-slim

# Evitar que Python escriba archivos .pyc en el disco
ENV PYTHONDONTWRITEBYTECODE=1
# Evitar que Python haga buffer a stdout/stderr
ENV PYTHONUNBUFFERED=1
# Host de uvicorn y puerto (override por env en plataformas que lo asignan solas)
ENV MONITOR_HOST="0.0.0.0"
# 8000 por default; si la plataforma inyecta $PORT, se pisa por env
ENV MONITOR_PORT="8000"
# Definir directorio de base de datos local para entorno Linux (dentro de /app)
ENV MONITOR_DB_DIR="/app/monitor"

WORKDIR /app

# Instalar fuentes libres (para fpdf2 y matplotlib export) y dependencias de compilacion si es necesario
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-liberation \
    fonts-dejavu-core \
    libfreetype6 \
    && rm -rf /var/lib/apt/lists/*

# Copiar dependencias
COPY requirements.txt .

# Instalar dependencias 
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el codigo del proyecto
COPY . .

# Exponer el puerto
EXPOSE ${MONITOR_PORT}

# Comando de ejecucion 
CMD ["python", "run.py"]
