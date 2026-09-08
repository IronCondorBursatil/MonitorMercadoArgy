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


# ── POST datos / permisos ───────────────────────────────────────────────────
@pytest.mark.noauth
def test_datos_guarda_normaliza_y_mantiene_la_ficha_abierta(usuarios):
    bob = _bob_id()
    with TestClient(app) as c:
        _login_admin(c)
        r = c.post(f"/users/{bob}/datos",
                   data={"full_name": "  Roberto Pérez ", "email": " Bob.Perez@Ejemplo.COM ",
                         "notes": "cliente"})
    assert r.status_code == 200 and f'action="/users/{bob}/datos"' in r.text
    with SessionLocal() as s:
        b = s.get(UserORM, bob)
        assert (b.full_name, b.email, b.notes) == ("Roberto Pérez", "bob.perez@ejemplo.com", "cliente")


@pytest.mark.noauth
def test_datos_rechaza_email_invalido_o_duplicado(usuarios):
    bob = _bob_id()
    with SessionLocal() as s:
        s.query(UserORM).filter(UserORM.username == "admin").first().email = "admin@ejemplo.com"
        s.commit()
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post(f"/users/{bob}/datos", data={"email": "sin-arroba"}).status_code == 400
        assert c.post(f"/users/{bob}/datos", data={"email": "ADMIN@ejemplo.com"}).status_code == 400
        assert c.post("/users/999999/datos", data={"email": ""}).status_code == 404
        # vaciar el email es válido
        assert c.post(f"/users/{bob}/datos", data={"email": ""}).status_code == 200
    with SessionLocal() as s:
        assert s.get(UserORM, bob).email is None


@pytest.mark.noauth
def test_permisos_reemplaza_a_update_y_conserva_los_guards(usuarios):
    bob = _bob_id()
    with SessionLocal() as s:
        admin_id = s.query(UserORM).filter(UserORM.username == "admin").first().id
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post(f"/users/{bob}/permisos", data={"tabs": ["bonos", "fci", "inventada"]}).status_code == 200
        r = c.post(f"/users/{admin_id}/permisos", data={"is_admin": "false", "tabs": ["bonos"]})
        assert "último administrador" in r.text
        assert c.post("/users/999999/permisos", data={"tabs": ["bonos"]}).status_code == 404
    with SessionLocal() as s:
        assert s.get(UserORM, bob).allowed_tabs == ["bonos", "fci"]     # la inventada no entra
        assert s.get(UserORM, admin_id).is_admin is True


# ── reset manual / cerrar sesiones ──────────────────────────────────────────
@pytest.mark.noauth
def test_reset_manual_cambia_la_clave_fecha_y_cierra_sesiones(usuarios):
    bob = _bob_id()
    with TestClient(app) as bob_c, TestClient(app) as admin_c:
        assert _login(bob_c, "bob", "bobpass1234").status_code in (302, 303)
        _login_admin(admin_c)
        r = admin_c.post(f"/users/{bob}/reset", data={"channel": "manual", "password": "nuevaclave1"})
        assert r.status_code == 200
        assert bob_c.get("/", follow_redirects=False).status_code == 302   # sesión muerta
        assert _login(bob_c, "bob", "nuevaclave1").status_code in (302, 303)
    with SessionLocal() as s:
        assert s.get(UserORM, bob).password_changed_at is not None


@pytest.mark.noauth
def test_reset_valida_despues_del_lookup_y_rechaza_canales_futuros(usuarios):
    bob = _bob_id()
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post("/users/999999/reset", data={"channel": "manual", "password": "x"}).status_code == 404
        assert c.post(f"/users/{bob}/reset", data={"channel": "manual", "password": "x"}).status_code == 400
        assert c.post(f"/users/{bob}/reset", data={"channel": "mail"}).status_code == 400


@pytest.mark.noauth
def test_cerrar_sesiones_saca_al_usuario_sin_cambiarle_la_clave(usuarios):
    bob = _bob_id()
    with TestClient(app) as bob_c, TestClient(app) as admin_c:
        assert _login(bob_c, "bob", "bobpass1234").status_code in (302, 303)
        _login_admin(admin_c)
        assert admin_c.post(f"/users/{bob}/sesiones/cerrar").status_code == 200
        assert bob_c.get("/", follow_redirects=False).status_code == 302
        assert _login(bob_c, "bob", "bobpass1234").status_code in (302, 303)   # misma clave
        assert admin_c.post("/users/999999/sesiones/cerrar").status_code == 404


# ── estado ──────────────────────────────────────────────────────────────────
@pytest.mark.noauth
def test_deshabilitar_y_habilitar_desde_el_manager(usuarios):
    bob = _bob_id()
    with TestClient(app) as bob_c, TestClient(app) as admin_c:
        assert _login(bob_c, "bob", "bobpass1234").status_code in (302, 303)
        _login_admin(admin_c)
        r = admin_c.post(f"/users/{bob}/estado", data={"activo": "0"})
        assert r.status_code == 200 and "Habilitar cuenta" in r.text
        assert bob_c.get("/", follow_redirects=False).status_code == 302
        assert _login(bob_c, "bob", "bobpass1234").status_code == 200        # no entra
        r = admin_c.post(f"/users/{bob}/estado", data={"activo": "1"})
        assert r.status_code == 200 and "Deshabilitar cuenta" in r.text
        assert _login(bob_c, "bob", "bobpass1234").status_code in (302, 303)
        assert admin_c.post("/users/999999/estado", data={"activo": "0"}).status_code == 404


@pytest.mark.noauth
def test_no_se_puede_deshabilitar_a_uno_mismo_y_siempre_queda_un_admin_activo(usuarios):
    """El guard de "uno mismo" es el que se ejercita: como quien pide es un admin ACTIVO,
    el único caso de "último admin activo" es deshabilitarse a sí mismo. El guard de
    último admin del handler queda como defensa en profundidad (p. ej. con el admin
    falso de `_auth_bypass`, que no vive en la DB)."""
    with SessionLocal() as s:
        admin_id = s.query(UserORM).filter(UserORM.username == "admin").first().id
        s.add(UserORM(username="admin2", hashed_password=get_password_hash("adminpass2"),
                      is_admin=True, allowed_tabs=["*"], is_active=True))
        s.commit()
        admin2_id = s.query(UserORM).filter(UserORM.username == "admin2").first().id
    with TestClient(app) as c:
        _login_admin(c)
        r = c.post(f"/users/{admin_id}/estado", data={"activo": "0"})
        assert r.status_code == 400 and "tu propia cuenta" in r.text
        assert c.post(f"/users/{admin2_id}/estado", data={"activo": "0"}).status_code == 200
        # admin2 quedó inactivo → admin es el ÚLTIMO admin activo; con admin2 logueado
        # no se lo podría deshabilitar. Se simula desde admin2 rehabilitado:
        c.post(f"/users/{admin2_id}/estado", data={"activo": "1"})
    with TestClient(app) as c2:
        assert _login(c2, "admin2", "adminpass2").status_code in (302, 303)
        c2.post(f"/users/{admin_id}/estado", data={"activo": "0"})           # deja a admin2 solo
        r = c2.post(f"/users/{admin2_id}/estado", data={"activo": "0"})
        assert r.status_code == 400 and "tu propia cuenta" in r.text
    with SessionLocal() as s:
        activos = s.query(UserORM).filter(UserORM.is_admin.is_(True), UserORM.is_active.is_(True)).count()
        assert activos >= 1


@pytest.mark.noauth
def test_un_usuario_comun_no_puede_tocar_el_manager(usuarios):
    """Todas las rutas del Manager (ya existen todas al llegar acá) exigen admin: un
    usuario común logueado recibe 403, nunca 302 ni 200."""
    bob = _bob_id()
    with TestClient(app) as c:
        assert _login(c, "bob", "bobpass1234").status_code in (302, 303)
        for path in (f"/users/{bob}/datos", f"/users/{bob}/permisos", f"/users/{bob}/reset",
                     f"/users/{bob}/sesiones/cerrar", f"/users/{bob}/estado", "/users/add"):
            r = c.post(path, data={"activo": "1", "channel": "manual", "password": "x" * 10,
                                   "username": "z", "tabs": ["bonos"]}, follow_redirects=False)
            assert r.status_code == 403, f"{path} devolvió {r.status_code}"
        assert c.get("/users", follow_redirects=False).status_code == 403
        assert c.get(f"/users/{bob}/ficha", follow_redirects=False).status_code == 403
