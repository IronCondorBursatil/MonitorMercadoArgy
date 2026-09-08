"""Manager de usuarios v2 (Fase 1): schema, reglas puras, is_active, rutas del admin.
Spec: docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md."""

from datetime import date, datetime

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from apps.web.app import app
from apps.web.routers import auth as auth_router
from core.infrastructure.db.engine import get_engine, SessionLocal
from core.infrastructure.db.models import Base, UserORM
from core.security import get_password_hash


def test_columnas_nuevas_entran_por_migracion_forward_only(tmp_path):
    """Sobre una tabla `users` PREEXISTENTE (schema viejo, con una fila), `init_db`
    agrega las columnas con ALTER, la fila sobrevive y queda ACTIVA (default 1)."""
    from config.settings import settings as _s
    from core.infrastructure.db import engine as db_engine
    from core.infrastructure.db.catalog_repository import init_db

    db = tmp_path / "vieja.db"
    eng = sa.create_engine(f"sqlite:///{db}")
    with eng.begin() as con:
        con.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "username VARCHAR NOT NULL, hashed_password VARCHAR NOT NULL, "
            "is_admin BOOLEAN, allowed_tabs JSON, token_version INTEGER)")
        con.exec_driver_sql(
            "INSERT INTO users (username, hashed_password, is_admin, allowed_tabs, token_version) "
            "VALUES ('viejo', 'x', 0, '[]', 0)")
    eng.dispose()

    db_engine.configure(db)
    try:
        init_db()
        with SessionLocal() as s:
            fila = s.query(UserORM).filter(UserORM.username == "viejo").first()
            assert fila is not None, "la fila vieja no sobrevivió"
            assert fila.is_active is True, "una fila vieja tiene que quedar ACTIVA"
            assert fila.email is None and fila.full_name is None
            assert fila.last_login_at is None and fila.created_at is None
    finally:
        db_engine.configure(_s.catalog_db)


def test_email_es_unico_solo_cuando_no_es_nulo(tmp_path):
    """Dos usuarios SIN email conviven; dos con el MISMO email no (índice parcial)."""
    from config.settings import settings as _s
    from core.infrastructure.db import engine as db_engine
    from core.infrastructure.db.catalog_repository import init_db

    db_engine.configure(tmp_path / "nueva.db")
    try:
        init_db()
        with SessionLocal() as s:
            s.add(UserORM(username="a", hashed_password="x", is_admin=False, allowed_tabs=[]))
            s.add(UserORM(username="b", hashed_password="x", is_admin=False, allowed_tabs=[]))
            s.commit()                       # dos NULL: OK
            s.add(UserORM(username="c", hashed_password="x", is_admin=False, allowed_tabs=[],
                          email="dup@ejemplo.com"))
            s.commit()
            s.add(UserORM(username="d", hashed_password="x", is_admin=False, allowed_tabs=[],
                          email="dup@ejemplo.com"))
            try:
                s.commit()
                raise AssertionError("dos usuarios con el mismo email pasaron")
            except sa.exc.IntegrityError:
                s.rollback()
    finally:
        db_engine.configure(_s.catalog_db)


def test_password_invalida_es_la_politica_unica():
    from core.security import password_invalida

    assert password_invalida("corta") is not None
    assert password_invalida("a" * 9) is not None
    assert password_invalida("a" * 10) is None
    assert password_invalida("á" * 40) is not None      # 80 bytes > 72: bcrypt truncaría
    assert password_invalida("a" * 72) is None


def _u(**kw):
    base = dict(username="mcaceres", hashed_password="x", is_admin=False,
                allowed_tabs=["bonos", "on"], is_active=True)
    base.update(kw)
    return UserORM(**base)


def test_normalizar_email():
    from apps.web.users_service import normalizar_email
    assert normalizar_email("  M.Caceres@Ejemplo.com ") == "m.caceres@ejemplo.com"
    assert normalizar_email("") is None
    assert normalizar_email(None) is None


def test_email_invalido():
    from apps.web.users_service import email_invalido
    assert email_invalido(None) == ""                       # sin email es válido
    assert email_invalido("m.caceres@ejemplo.com") == ""
    assert email_invalido("sin-arroba") != ""
    assert email_invalido("a@b") != ""                      # dominio sin punto
    assert email_invalido("a@.com") != ""
    assert email_invalido('x"onmouseover=1@ejemplo.com') != ""   # mismos prohibidos que el username
    assert email_invalido("a" * 250 + "@x.co") != ""        # > 254


def test_fmt_momento():
    from apps.web.users_service import fmt_momento
    hoy = date(2026, 9, 8)
    assert fmt_momento(None, hoy) == "—"
    assert fmt_momento(datetime(2026, 9, 8, 9, 12), hoy) == "hoy 09:12"
    assert fmt_momento(datetime(2026, 9, 7, 18, 40), hoy) == "ayer 18:40"
    assert fmt_momento(datetime(2026, 8, 12, 10, 0), hoy) == "12 ago"
    assert fmt_momento(datetime(2025, 9, 3, 10, 0), hoy) == "03 sep 2025"


def test_vista_usuario_y_estado():
    from apps.web.users_service import estado_usuario, iniciales, vista_usuario
    u = _u(full_name="Mariana Cáceres", email=None, last_login_at=datetime(2026, 9, 8, 9, 12))
    assert iniciales(u) == "MC"
    assert iniciales(_u(full_name=None)) == "M"
    assert estado_usuario(u) == "activo"
    assert estado_usuario(_u(is_active=False)) == "deshabilitado"
    v = vista_usuario(u, hoy=date(2026, 9, 8))
    assert v["sin_email"] is True and v["ultimo_acceso"] == "hoy 09:12"
    assert v["tabs"] == ["bonos", "on"]
    assert vista_usuario(_u(is_admin=True, allowed_tabs=["*"]))["tabs"] == []


def test_actividad_reciente_derivada_y_ordenada():
    from apps.web.users_service import actividad_reciente
    u = _u(created_at=datetime(2026, 7, 15, 11, 20), created_by="admin",
           password_changed_at=datetime(2026, 8, 21, 10, 3),
           last_login_at=datetime(2026, 9, 8, 9, 12), last_login_ip="181.1.2.3")
    ev = actividad_reciente(u, hoy=date(2026, 9, 8))
    assert [e["que"] for e in ev] == ["Ingreso", "Contraseña cambiada", "Alta"]
    assert ev[0]["texto"] == "hoy 09:12" and ev[0]["detalle"] == "181.1.2.3"
    assert ev[2]["detalle"] == "por admin"
    assert actividad_reciente(_u()) == []


def test_resumen():
    from apps.web.users_service import resumen
    r = resumen([_u(), _u(is_active=False), _u(email="a@b.co")])
    assert r == {"total": 3, "activos": 2, "deshabilitados": 1, "sin_email": 2}


@pytest.fixture
def usuarios():
    """admin + bob (sólo 'bonos') en la DB de test; limiter del login limpio."""
    Base.metadata.create_all(bind=get_engine())
    auth_router._login_attempts.clear()
    with SessionLocal() as s:
        s.query(UserORM).delete()
        s.add(UserORM(username="admin", hashed_password=get_password_hash("adminpass1"),
                      is_admin=True, allowed_tabs=["*"], is_active=True))
        s.add(UserORM(username="bob", hashed_password=get_password_hash("bobpass1234"),
                      is_admin=False, allowed_tabs=["bonos"], is_active=True,
                      email="bob@ejemplo.com", full_name="Bob Pérez"))
        s.commit()
    yield
    with SessionLocal() as s:
        s.query(UserORM).delete()
        s.commit()
    auth_router._login_attempts.clear()


def _login(c, user, pw):
    return c.post("/login", data={"username": user, "password": pw}, follow_redirects=False)


def _login_admin(c):
    r = _login(c, "admin", "adminpass1")
    assert r.status_code in (302, 303)


def _bob_id():
    with SessionLocal() as s:
        return s.query(UserORM).filter(UserORM.username == "bob").first().id


def _set_bob(**kw):
    with SessionLocal() as s:
        bob = s.query(UserORM).filter(UserORM.username == "bob").first()
        for k, v in kw.items():
            setattr(bob, k, v)
        s.commit()


# ── is_active ───────────────────────────────────────────────────────────────
@pytest.mark.noauth
def test_usuario_deshabilitado_no_entra_y_recibe_el_mismo_mensaje(usuarios):
    """Mutación: si se saca el chequeo de is_active en routers/auth.login, esto da 302."""
    _set_bob(is_active=False)
    with TestClient(app) as c:
        r_off = _login(c, "bob", "bobpass1234")
        r_mal = _login(c, "bob", "clave-incorrecta")
    assert r_off.status_code == 200 and "access_token" not in r_off.cookies
    assert "Usuario o contraseña incorrectos" in r_off.text
    assert r_off.text == r_mal.text, "deshabilitado y clave incorrecta tienen que verse IGUAL"


@pytest.mark.noauth
def test_deshabilitar_mata_la_sesion_viva(usuarios):
    """La cookie sigue siendo válida (misma token_version) pero deps_auth rechaza al
    inactivo. Mutación: sacar el chequeo en `_get_user_from_token` → 200."""
    with TestClient(app) as c:
        assert _login(c, "bob", "bobpass1234").status_code in (302, 303)
        assert c.get("/", follow_redirects=False).status_code == 200
        _set_bob(is_active=False)
        assert c.get("/", follow_redirects=False).status_code == 302


@pytest.mark.noauth
def test_login_exitoso_registra_ultimo_ingreso(usuarios):
    with TestClient(app) as c:
        assert _login(c, "bob", "bobpass1234").status_code in (302, 303)
    with SessionLocal() as s:
        bob = s.query(UserORM).filter(UserORM.username == "bob").first()
        assert bob.last_login_at is not None
        assert (datetime.now() - bob.last_login_at).total_seconds() < 60
        assert bob.last_login_ip                       # 'testclient' en TestClient


# ── GET /users y ficha ──────────────────────────────────────────────────────
@pytest.mark.noauth
def test_la_tabla_muestra_email_estado_y_link_a_la_ficha(usuarios):
    bob = _bob_id()
    with TestClient(app) as c:
        _login_admin(c)
        r = c.get("/users")
    assert r.status_code == 200
    assert "bob@ejemplo.com" in r.text and "Bob Pérez" in r.text
    assert f'hx-get="/users/{bob}/ficha"' in r.text
    assert "Elegí un usuario" in r.text            # sin selección: panel vacío


@pytest.mark.noauth
def test_u_en_la_query_precarga_la_ficha(usuarios):
    bob = _bob_id()
    with TestClient(app) as c:
        _login_admin(c)
        r = c.get(f"/users?u={bob}")
    assert r.status_code == 200
    assert f'action="/users/{bob}/datos"' in r.text
    assert f'action="/users/{bob}/permisos"' in r.text
    assert "Elegí un usuario" not in r.text


@pytest.mark.noauth
def test_la_ficha_es_un_fragmento(usuarios):
    bob = _bob_id()
    with TestClient(app) as c:
        _login_admin(c)
        r = c.get(f"/users/{bob}/ficha")
        r404 = c.get("/users/999999/ficha")
    assert r.status_code == 200
    assert "<html" not in r.text.lower() and "Permisos" in r.text
    assert f'action="/users/{bob}/sesiones/cerrar"' in r.text
    assert f'action="/users/{bob}/estado"' in r.text
    assert r404.status_code == 404
