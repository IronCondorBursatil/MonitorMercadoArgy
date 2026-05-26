"""Levanta la app FastAPI en :8001 para verificación (no toca el :8000 productivo).

    & "$env:LOCALAPPDATA\\Microsoft\\WindowsApps\\python3.12.exe" scripts/run_server_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("apps.web.app:app", host="127.0.0.1", port=8001, log_config=None)
