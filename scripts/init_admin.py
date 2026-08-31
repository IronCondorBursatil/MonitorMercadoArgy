import os
import sys

# Añadir el path raíz para que funcionen los imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.infrastructure.db.engine import SessionLocal, get_engine
from core.infrastructure.db.models import Base, UserORM
from core.security import get_password_hash

def init_admin():
    print("Creando tablas si no existen...")
    Base.metadata.create_all(bind=get_engine())

    db = SessionLocal()

    existing = db.query(UserORM).filter(UserORM.username == "admin").first()
    if existing:
        print("El usuario 'admin' ya existe. No se hicieron cambios.")
    else:
        # Sin default hardcodeado: 'admin123' quedaba activo en prod y es la primera
        # entrada de cualquier wordlist. La password se pasa por env.
        password = os.environ.get("MONITOR_ADMIN_PASSWORD")
        if not password:
            db.close()
            sys.exit("Definí MONITOR_ADMIN_PASSWORD (la password del primer admin) "
                     "antes de correr init_admin.")
        print("Creando usuario 'admin'...")
        admin = UserORM(
            username="admin",
            hashed_password=get_password_hash(password),
            is_admin=True
        )
        db.add(admin)
        db.commit()
        print("Usuario administrador creado exitosamente.")

    db.close()

if __name__ == "__main__":
    init_admin()
