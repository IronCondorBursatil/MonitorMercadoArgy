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
        print("Creando usuario 'admin' (password: admin123)...")
        admin = UserORM(
            username="admin",
            hashed_password=get_password_hash("admin123"),
            is_admin=True
        )
        db.add(admin)
        db.commit()
        print("Usuario administrador creado exitosamente.")
        
    db.close()

if __name__ == "__main__":
    init_admin()
