import os
import sys
import sqlite3
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from config.settings import settings

def migrate():
    db_path = settings.catalog_db
    if not os.path.exists(db_path):
        print(f"La base de datos {db_path} no existe. No hay nada que migrar.")
        return

    print(f"Migrando {db_path}...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Comprobar si la columna existe
    cursor.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in cursor.fetchall()]

    if "allowed_tabs" not in columns:
        print("Añadiendo columna 'allowed_tabs' a la tabla 'users'...")
        cursor.execute("ALTER TABLE users ADD COLUMN allowed_tabs JSON")
        # Por defecto el admin (is_admin=1) tiene todo
        default_tabs = json.dumps(["*"])
        cursor.execute("UPDATE users SET allowed_tabs = ? WHERE is_admin = 1", (default_tabs,))
        # Usuarios normales sin tabs hasta que el admin los edite
        empty_tabs = json.dumps([])
        cursor.execute("UPDATE users SET allowed_tabs = ? WHERE is_admin = 0", (empty_tabs,))
        conn.commit()
        print("Migración completada con éxito.")
    else:
        print("La columna 'allowed_tabs' ya existe.")

    conn.close()

if __name__ == "__main__":
    migrate()
