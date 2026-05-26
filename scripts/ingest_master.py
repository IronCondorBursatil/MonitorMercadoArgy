"""Excel → SQLite. Idempotente. Correr 1× y cada vez que edites el master a mano:

    & "$env:LOCALAPPDATA\\Microsoft\\WindowsApps\\python3.12.exe" scripts/ingest_master.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings  # noqa: E402
from core.infrastructure.db.catalog_repository import ingest_from_excel  # noqa: E402


def main() -> None:
    n = ingest_from_excel(str(settings.master_xlsx))
    print(f"Seed OK - {n} instruments -> {settings.catalog_db}")


if __name__ == "__main__":
    main()
