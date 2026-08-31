"""Persistencia SQLite (catálogo de instrumentos) vía SQLAlchemy 2.0.

Las bases `.db` viven FUERA del working tree de git (%LOCALAPPDATA%\\monitor) — ver
config.settings.settings.catalog_db. El Excel pasa a ser sólo semilla
(scripts/ingest_master.py).
"""
