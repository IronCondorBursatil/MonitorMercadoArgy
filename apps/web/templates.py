"""Singleton de Jinja2Templates compartido por todos los routers.

Evita que cada router construya su propia instancia apuntando al mismo
directorio — misma ruta resuelta que el patrón anterior (parent.parent/templates).
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
