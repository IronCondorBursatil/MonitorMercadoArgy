"""Levanta el server en port 8001 para verificación (no toca el 8000 productivo)."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.web import server
server.PORT = 8001
server.main()
