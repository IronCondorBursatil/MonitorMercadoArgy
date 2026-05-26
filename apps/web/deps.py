"""Dependencias FastAPI (Depends): repo, engine, state, hub.

`get_repo` devuelve el CatalogRepository (SQLite, Fase 2) — éste es el punto del
cutover runtime: la web nueva lee instrumentos de SQLite, no del Excel.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Request

from core.infrastructure.db.catalog_repository import CatalogRepository


@lru_cache(maxsize=1)
def get_repo() -> CatalogRepository:
    """Singleton del CatalogRepository (auto-siembra desde Excel si la .db está vacía)."""
    return CatalogRepository()


def get_state(request: Request):
    return request.app.state.app_state


def get_hub(request: Request):
    return request.app.state.hub
