"""Manager de usuarios v2 (Fase 1): schema, reglas puras, is_active, rutas del admin.
Spec: docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md."""

from datetime import date, datetime, timedelta

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


@pytest.fixture(autouse=True)
def limiters_limpios():
    """Los baldes de /forgot y /reset son globales del módulo `auth`: limpios antes y después
    de CADA test, para que ninguno dependa del orden ni le deje un 429 armado al siguiente."""
    baldes = (auth_router._forgot_attempts_ip, auth_router._forgot_attempts_dato,
              auth_router._reset_attempts)
    for b in baldes:
        b.clear()
    yield
    for b in baldes:
        b.clear()


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


# ── alta ────────────────────────────────────────────────────────────────────
@pytest.mark.noauth
def test_alta_guarda_perfil_y_trazabilidad(usuarios):
    with TestClient(app) as c:
        _login_admin(c)
        r = c.post("/users/add", data={"username": "mcaceres", "password": "clave-segura-1",
                                       "full_name": "Mariana Cáceres", "email": "M.Caceres@Ejemplo.com",
                                       "notes": "cliente", "tabs": ["bonos", "fci"]})
    assert r.status_code == 200 and 'action="/users/' in r.text and "mcaceres" in r.text
    with SessionLocal() as s:
        m = s.query(UserORM).filter(UserORM.username == "mcaceres").first()
        assert m.email == "m.caceres@ejemplo.com" and m.full_name == "Mariana Cáceres"
        assert m.created_by == "admin" and m.created_at is not None
        assert m.password_changed_at is not None and m.is_active is True
        assert m.allowed_tabs == ["bonos", "fci"]


@pytest.mark.noauth
def test_alta_rechaza_duplicados_y_email_invalido(usuarios):
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post("/users/add", data={"username": "bob", "password": "clave-segura-1"}).status_code == 400
        assert c.post("/users/add", data={"username": "otro", "password": "clave-segura-1",
                                          "email": "bob@ejemplo.com"}).status_code == 400
        assert c.post("/users/add", data={"username": "otro", "password": "clave-segura-1",
                                          "email": "mal"}).status_code == 400
    with SessionLocal() as s:
        assert s.query(UserORM).filter(UserORM.username == "otro").first() is None


# ── login con el look de la app ─────────────────────────────────────────────
@pytest.mark.noauth
def test_login_usa_los_tokens_y_el_header_de_la_app(usuarios):
    with TestClient(app) as c:
        r = c.get("/login")
    assert r.status_code == 200
    assert "/static/css/app.css" in r.text
    assert "MONITOR · Renta Fija AR" in r.text
    for var in ("var(--bg)", "var(--surface)", "var(--border)"):
        assert var not in r.text, f"login.html sigue usando la variable inexistente {var}"
    assert "/health/badge" not in r.text and "hx-get" not in r.text   # sin nav ni polling privado


# ── review final I-1: los guards de último admin miran is_active ────────────
@pytest.mark.noauth
def test_no_se_puede_dejar_el_sistema_sin_admins_activos(usuarios):
    """A deshabilita a B y después intenta degradarse o borrarse: los guards de
    /permisos y /delete tienen que contar sólo admins ACTIVOS (antes contaban a B y
    dejaban pasar → cero admins que pudieran entrar). Mutación: sacar el filtro
    is_active de cualquiera de los dos counts pone esto en rojo."""
    with SessionLocal() as s:
        admin_id = s.query(UserORM).filter(UserORM.username == "admin").first().id
        s.add(UserORM(username="admin2", hashed_password=get_password_hash("adminpass2"),
                      is_admin=True, allowed_tabs=["*"], is_active=True))
        s.commit()
        admin2_id = s.query(UserORM).filter(UserORM.username == "admin2").first().id
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post(f"/users/{admin2_id}/estado", data={"activo": "0"}).status_code == 200
        r = c.post(f"/users/{admin_id}/permisos", data={"is_admin": "false", "tabs": ["bonos"]})
        assert "último administrador" in r.text, r.status_code
        r = c.post(f"/users/delete/{admin_id}")
        assert "último administrador" in r.text, r.status_code
    with SessionLocal() as s:
        activos = s.query(UserORM).filter(UserORM.is_admin.is_(True),
                                          UserORM.is_active.is_(True)).count()
        assert activos >= 1
        assert s.get(UserORM, admin_id).is_admin is True


@pytest.mark.noauth
def test_un_admin_no_puede_quitarse_el_rol_a_si_mismo(usuarios):
    """Con OTRO admin activo el guard de último admin no aplica; el de uno mismo sí."""
    with SessionLocal() as s:
        admin_id = s.query(UserORM).filter(UserORM.username == "admin").first().id
        s.add(UserORM(username="admin2", hashed_password=get_password_hash("adminpass2"),
                      is_admin=True, allowed_tabs=["*"], is_active=True))
        s.commit()
    with TestClient(app) as c:
        _login_admin(c)
        r = c.post(f"/users/{admin_id}/permisos", data={"is_admin": "false", "tabs": ["bonos"]})
        assert r.status_code == 400 and "vos mismo" in r.text
    with SessionLocal() as s:
        assert s.get(UserORM, admin_id).is_admin is True


# ── login por usuario o email ───────────────────────────────────────────────
@pytest.mark.noauth
def test_login_acepta_el_email_ademas_del_usuario(usuarios):
    with TestClient(app) as c:
        r = _login(c, "Bob@Ejemplo.com", "bobpass1234")      # mayúsculas: se normaliza
        assert r.status_code in (302, 303) and "access_token" in c.cookies
        assert c.get("/", follow_redirects=False).status_code == 200
    with SessionLocal() as s:
        assert s.query(UserORM).filter(UserORM.username == "bob").first().last_login_at is not None


@pytest.mark.noauth
def test_login_con_email_desconocido_o_clave_mal_es_indistinguible(usuarios):
    with TestClient(app) as c:
        r1 = _login(c, "nadie@ejemplo.com", "bobpass1234")
        r2 = _login(c, "bob@ejemplo.com", "clave-incorrecta")
        r3 = _login(c, "bob", "clave-incorrecta")
    assert r1.status_code == r2.status_code == r3.status_code == 200
    assert r1.text == r2.text == r3.text
    assert "access_token" not in c.cookies


# ── canal a mano editable ───────────────────────────────────────────────────
@pytest.mark.noauth
def test_el_modal_de_clave_manual_permite_tipearla(usuarios):
    with TestClient(app) as c:
        _login_admin(c)
        html = c.get("/users").text
    i = html.index("function resetPassword(")
    modal = html[i:i + 4000]
    assert 'id="rp-pwd"' in modal and "readonly" not in modal.split('id="rp-pwd"')[1][:200], (
        "el campo de la contraseña tiene que ser editable")
    assert 'id="rp-gen"' in modal and "Generar otra" in modal
    assert "input.value.length < 10" in modal or "value.length < 10" in modal


# ── tokens de reseteo ───────────────────────────────────────────────────────
def test_verify_password_no_explota_con_el_centinela_sin_clave():
    from core.security import SIN_PASSWORD_HASH, verify_password
    assert SIN_PASSWORD_HASH == "!"
    assert verify_password("cualquier-cosa", SIN_PASSWORD_HASH) is False
    assert verify_password("x", "") is False
    assert verify_password("x", "hash-que-no-es-bcrypt") is False


def test_token_nuevo_es_aleatorio_y_se_guarda_hasheado():
    from core.security import hash_token, new_reset_token
    a, b = new_reset_token(), new_reset_token()
    assert a != b and len(a) >= 40
    assert hash_token(a) != a and len(hash_token(a)) == 64 and hash_token(a) == hash_token(a)


@pytest.mark.noauth
def test_issue_lookup_consume_y_un_solo_uso(usuarios):
    from apps.web import reset_service as rs
    from core.infrastructure.db.models import PasswordResetTokenORM
    with SessionLocal() as s:
        bob = s.query(UserORM).filter(UserORM.username == "bob").first()
        t1 = rs.issue_reset_token(s, bob, purpose="reset", channel="link", by="admin")
        t2 = rs.issue_reset_token(s, bob, purpose="reset", channel="link", by="admin")
        assert rs.lookup_reset_token(s, t1) is None, "emitir uno nuevo invalida el anterior"
        user, row = rs.lookup_reset_token(s, t2)
        assert user.id == bob.id and row.purpose == "reset" and row.channel == "link" and row.created_by == "admin"
        assert timedelta(minutes=59) < (row.expires_at - row.created_at) <= timedelta(minutes=60)
        hash_antes, ver_antes = bob.hashed_password, bob.token_version or 0
        assert rs.consume_reset_token(s, t2, "clave-nueva-123").id == bob.id
        s.refresh(bob)
        assert bob.hashed_password != hash_antes and bob.token_version == ver_antes + 1
        assert bob.password_changed_at is not None
        assert rs.lookup_reset_token(s, t2) is None, "un token consumido no vuelve a servir"
        assert rs.consume_reset_token(s, t2, "otra-clave-1234") is None
        assert s.query(PasswordResetTokenORM).filter(PasswordResetTokenORM.token_hash == t2).count() == 0, \
            "el token en claro NUNCA se persiste"
        assert rs.lookup_reset_token(s, "token-inventado") is None


@pytest.mark.noauth
def test_token_vencido_o_de_usuario_deshabilitado_no_vale(usuarios):
    from apps.web import reset_service as rs
    from core.infrastructure.db.models import PasswordResetTokenORM
    with SessionLocal() as s:
        bob = s.query(UserORM).filter(UserORM.username == "bob").first()
        t = rs.issue_reset_token(s, bob, purpose="invite", channel="link", by="admin")
        row = s.query(PasswordResetTokenORM).filter(PasswordResetTokenORM.used_at.is_(None)).one()
        assert timedelta(hours=71) < (row.expires_at - row.created_at) <= timedelta(hours=72)
        row.expires_at = datetime.now() - timedelta(seconds=1)
        s.commit()
        assert rs.lookup_reset_token(s, t) is None
        t2 = rs.issue_reset_token(s, bob, purpose="reset", channel="link", by="admin")
        bob.is_active = False
        s.commit()
        assert rs.lookup_reset_token(s, t2) is None


@pytest.mark.noauth
def test_invitaciones_vivas_tokens_de_y_link(usuarios):
    from apps.web import reset_service as rs
    with SessionLocal() as s:
        bob = s.query(UserORM).filter(UserORM.username == "bob").first()
        assert rs.invitaciones_vivas(s) == {}
        rs.issue_reset_token(s, bob, purpose="invite", channel="link", by="admin")
        vivas = rs.invitaciones_vivas(s)
        assert list(vivas) == [bob.id] and vivas[bob.id].purpose == "invite"
        rs.issue_reset_token(s, bob, purpose="reset", channel="link", by="admin")
        assert rs.invitaciones_vivas(s) == {}, "el reset nuevo invalidó la invitación"
        assert [x.purpose for x in rs.tokens_de(s, bob)] == ["reset", "invite"]

    class _Req:
        base_url = "http://testserver/"
    from config.settings import settings
    viejo = settings.public_url
    try:
        settings.public_url = ""
        assert rs.reset_link(_Req(), "abc") == "http://testserver/reset/abc"
        settings.public_url = "http://129.80.148.166/"
        assert rs.reset_link(_Req(), "abc") == "http://129.80.148.166/reset/abc"
    finally:
        settings.public_url = viejo


@pytest.mark.noauth
def test_issue_invalida_tambien_los_objetos_ya_cargados_en_la_misma_sesion(usuarios):
    """Fix del review de la Task 3: con synchronize_session=False el objeto ya cargado en la
    sesión seguía con used_at=None después de emitir un token nuevo (identity map stale)."""
    from apps.web import reset_service as rs
    from core.infrastructure.db.models import PasswordResetTokenORM
    with SessionLocal() as s:
        bob = s.query(UserORM).filter(UserORM.username == "bob").first()
        rs.issue_reset_token(s, bob, purpose="invite", channel="link", by="admin")
        viva = s.query(PasswordResetTokenORM).filter(PasswordResetTokenORM.used_at.is_(None)).one()
        rs.issue_reset_token(s, bob, purpose="reset", channel="link", by="admin")   # invalida `viva`
        assert viva.used_at is not None, "el objeto ya cargado tiene que ver la invalidación"


# ── /reset/{token} ──────────────────────────────────────────────────────────
def _token_de_bob(purpose="reset"):
    from apps.web import reset_service as rs
    with SessionLocal() as s:
        bob = s.query(UserORM).filter(UserORM.username == "bob").first()
        return rs.issue_reset_token(s, bob, purpose=purpose, channel="link", by="admin")


@pytest.mark.noauth
def test_reset_get_muestra_el_formulario_solo_con_token_valido(usuarios):
    t = _token_de_bob()
    with TestClient(app) as c:
        ok = c.get(f"/reset/{t}")
        malo = c.get("/reset/token-inventado")
    assert ok.status_code == 200 and 'name="password2"' in ok.text and "Bob" in ok.text and "bob" in ok.text
    assert "MONITOR · Renta Fija AR" in ok.text and "hx-get" not in ok.text
    assert malo.status_code == 200 and "ya no sirve" in malo.text and 'name="password"' not in malo.text


@pytest.mark.noauth
def test_reset_post_cambia_la_clave_cierra_sesiones_y_vuelve_al_login(usuarios):
    t = _token_de_bob()
    with TestClient(app) as bob_c, TestClient(app) as anon:
        assert _login(bob_c, "bob", "bobpass1234").status_code in (302, 303)
        r = anon.post(f"/reset/{t}", data={"password": "mi-clave-nueva-1", "password2": "mi-clave-nueva-1"},
                      follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?reset=ok"
        assert bob_c.get("/", follow_redirects=False).status_code == 302, "las otras sesiones tienen que morir"
        assert _login(anon, "bob", "mi-clave-nueva-1").status_code in (302, 303)
        # el mismo link, otra vez: ya no sirve (un solo uso). Mutación: sacar el chequeo de
        # used_at en lookup_reset_token pone esto en rojo.
        r2 = anon.post(f"/reset/{t}", data={"password": "otra-clave-9999", "password2": "otra-clave-9999"},
                       follow_redirects=False)
        assert r2.status_code == 200 and "ya no sirve" in r2.text
        banner = anon.get("/login?reset=ok")
        assert "Tu contraseña se actualizó" in banner.text
        assert "Tu contraseña se actualizó" not in anon.get("/login").text


@pytest.mark.noauth
def test_reset_post_rechaza_clave_corta_o_distinta_y_el_token_sigue_vivo(usuarios):
    t = _token_de_bob()
    with TestClient(app) as c:
        r1 = c.post(f"/reset/{t}", data={"password": "corta", "password2": "corta"})
        r2 = c.post(f"/reset/{t}", data={"password": "clave-larga-ok-1", "password2": "clave-larga-ok-2"})
        assert r1.status_code == 400 and 'name="password2"' in r1.text and "al menos 10" in r1.text
        assert r2.status_code == 400 and "no coinciden" in r2.text
        assert c.get(f"/reset/{t}").status_code == 200 and 'name="password2"' in c.get(f"/reset/{t}").text
    with SessionLocal() as s:
        assert s.query(UserORM).filter(UserORM.username == "bob").first().password_changed_at is None


@pytest.mark.noauth
def test_reset_de_usuario_deshabilitado_o_vencido_es_la_misma_pagina(usuarios):
    from datetime import timedelta
    from core.infrastructure.db.models import PasswordResetTokenORM
    t = _token_de_bob()
    _set_bob(is_active=False)
    with TestClient(app) as c:
        r_off = c.get(f"/reset/{t}")
    _set_bob(is_active=True)
    with SessionLocal() as s:
        s.query(PasswordResetTokenORM).update({"expires_at": datetime.now() - timedelta(seconds=1)})
        s.commit()
    with TestClient(app) as c:
        r_venc = c.get(f"/reset/{t}")
        r_nada = c.get("/reset/xyz")
    assert r_off.status_code == r_venc.status_code == r_nada.status_code == 200
    assert r_off.text == r_venc.text == r_nada.text


@pytest.mark.noauth
def test_reset_post_tiene_rate_limit_por_ip(usuarios):
    with TestClient(app) as c:
        codigos = [c.post("/reset/token-falso", data={"password": "x" * 12, "password2": "x" * 12}).status_code
                   for _ in range(11)]
    assert codigos[:10] == [200] * 10 and codigos[10] == 429


# ── canal link + actividad ──────────────────────────────────────────────────
def test_fmt_restante_y_estado_invitado():
    from datetime import timedelta
    from apps.web.users_service import estado_usuario, fmt_restante
    from core.security import SIN_PASSWORD_HASH
    ahora = datetime(2026, 9, 8, 10, 0)
    assert fmt_restante(ahora + timedelta(days=2, hours=3), ahora) == "vence en 2 d"
    assert fmt_restante(ahora + timedelta(hours=5, minutes=10), ahora) == "vence en 5 h"
    assert fmt_restante(ahora + timedelta(minutes=45), ahora) == "vence en 45 min"
    assert fmt_restante(ahora - timedelta(seconds=1), ahora) == "vencida"
    assert estado_usuario(_u(hashed_password=SIN_PASSWORD_HASH)) == "invitado"
    assert estado_usuario(_u(hashed_password=SIN_PASSWORD_HASH, is_active=False)) == "deshabilitado"


@pytest.mark.noauth
def test_canal_link_genera_un_link_que_funciona_y_queda_en_la_actividad(usuarios):
    import re
    bob = _bob_id()
    with TestClient(app) as admin_c, TestClient(app) as anon:
        _login_admin(admin_c)
        r = admin_c.post(f"/users/{bob}/reset", data={"channel": "link"})
        assert r.status_code == 200
        m = re.search(r'value="(http://testserver/reset/[A-Za-z0-9_\-]+)"', r.text)
        assert m, "el link tiene que mostrarse una vez en un input readonly"
        link = m.group(1)
        assert "Link de reseteo generado" in r.text and "60 minutos" in r.text
        assert anon.get(link.replace("http://testserver", "")).status_code == 200
        r2 = anon.post(link.replace("http://testserver", ""),
                       data={"password": "elegida-por-bob-1", "password2": "elegida-por-bob-1"},
                       follow_redirects=False)
        assert r2.status_code == 303
        ficha = admin_c.get(f"/users/{bob}/ficha").text
        assert "Contraseña elegida desde el link" in ficha
        assert "por un administrador" not in ficha, "el evento genérico duplicaría el del token"
        assert admin_c.post(f"/users/{bob}/reset", data={"channel": "mail"}).status_code == 400
        assert admin_c.post("/users/999999/reset", data={"channel": "link"}).status_code == 404


# ── alta por invitación ─────────────────────────────────────────────────────
@pytest.mark.noauth
def test_alta_por_invitacion_crea_sin_clave_y_entrega_el_link(usuarios):
    import re
    from core.security import SIN_PASSWORD_HASH
    with TestClient(app) as admin_c, TestClient(app) as anon:
        _login_admin(admin_c)
        r = admin_c.post("/users/add", data={"username": "jperez", "access": "invite", "full_name": "Juan Pérez",
                                             "tabs": ["bonos"]})
        assert r.status_code == 200 and "Invitación · vence en" in r.text and "72 horas" in r.text
        link = re.search(r'value="(http://testserver/reset/[A-Za-z0-9_\-]+)"', r.text).group(1)
        with SessionLocal() as s:
            j = s.query(UserORM).filter(UserORM.username == "jperez").first()
            assert j.hashed_password == SIN_PASSWORD_HASH and j.password_changed_at is None and j.is_active is True
        assert _login(anon, "jperez", "cualquier-cosa-1").status_code == 200, "el invitado no entra sin aceptar"
        r2 = anon.post(link.replace("http://testserver", ""),
                       data={"password": "clave-de-juan-1", "password2": "clave-de-juan-1"}, follow_redirects=False)
        assert r2.status_code == 303
        assert _login(anon, "jperez", "clave-de-juan-1").status_code in (302, 303)
        ficha = admin_c.get("/users").text
        assert "Invitación · vence en" not in ficha and "Invitación aceptada" in admin_c.get(
            f"/users/{j.id}/ficha").text


@pytest.mark.noauth
def test_alta_con_password_sigue_igual_y_la_invitacion_no_exige_password(usuarios):
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post("/users/add", data={"username": "conclave", "access": "password", "password": "clave-larga-1"}).status_code == 200
        assert c.post("/users/add", data={"username": "sinclave", "access": "password"}).status_code == 400
        assert c.post("/users/add", data={"username": "invitado2", "access": "invite"}).status_code == 200
        assert c.post("/users/add", data={"username": "raro", "access": "otro"}).status_code == 400


# ── security review: los gestos de revocación invalidan los links vivos ───────
@pytest.mark.noauth
def test_clave_manual_cerrar_sesiones_y_deshabilitar_invalidan_los_links_vivos(usuarios):
    """Un link filtrado no puede sobrevivir a la remediación del admin. El token queda ligado a
    la token_version del usuario al emitirlo; todo gesto que la suba lo mata. Mutación: sacar el
    chequeo de token_version en lookup_reset_token pone esto en rojo en los tres casos."""
    from apps.web import reset_service as rs
    bob = _bob_id()
    with TestClient(app) as admin_c, TestClient(app) as anon:
        _login_admin(admin_c)
        # 1) clave a mano
        t1 = _token_de_bob()
        assert 'name="password2"' in anon.get(f"/reset/{t1}").text
        assert admin_c.post(f"/users/{bob}/reset", data={"channel": "manual", "password": "otra-clave-9999"}).status_code == 200
        assert "ya no sirve" in anon.get(f"/reset/{t1}").text
        # 2) cerrar sesiones
        t2 = _token_de_bob()
        assert admin_c.post(f"/users/{bob}/sesiones/cerrar").status_code == 200
        assert "ya no sirve" in anon.get(f"/reset/{t2}").text
        # 3) deshabilitar y rehabilitar dentro del TTL
        t3 = _token_de_bob()
        assert admin_c.post(f"/users/{bob}/estado", data={"activo": "0"}).status_code == 200
        assert admin_c.post(f"/users/{bob}/estado", data={"activo": "1"}).status_code == 200
        assert "ya no sirve" in anon.get(f"/reset/{t3}").text
        r = anon.post(f"/reset/{t3}", data={"password": "hackeo-intento-1", "password2": "hackeo-intento-1"})
        assert r.status_code == 200 and "ya no sirve" in r.text
    with SessionLocal() as s:
        b = s.query(UserORM).filter(UserORM.username == "bob").first()
        assert rs.lookup_reset_token(s, t3) is None
        # un token nuevo sí sirve (la versión coincide)
        t4 = rs.issue_reset_token(s, b, purpose="reset", channel="link", by="admin")
        assert rs.lookup_reset_token(s, t4) is not None


@pytest.mark.noauth
def test_borrar_un_usuario_con_tokens_no_rompe_y_se_lleva_sus_tokens(usuarios):
    """Guardián del ondelete=CASCADE: sin él, con PRAGMA foreign_keys=ON el DELETE tira 500."""
    from core.infrastructure.db.models import PasswordResetTokenORM
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post("/users/add", data={"username": "efimero", "access": "invite"}).status_code == 200
        with SessionLocal() as s:
            uid = s.query(UserORM).filter(UserORM.username == "efimero").first().id
            assert s.query(PasswordResetTokenORM).filter(PasswordResetTokenORM.user_id == uid).count() == 1
        assert c.post(f"/users/delete/{uid}").status_code == 200
    with SessionLocal() as s:
        assert s.get(UserORM, uid) is None
        assert s.query(PasswordResetTokenORM).filter(PasswordResetTokenORM.user_id == uid).count() == 0


@pytest.mark.noauth
def test_el_token_no_aparece_en_la_auditoria(usuarios, caplog):
    import logging
    import re
    bob = _bob_id()
    with caplog.at_level(logging.INFO, logger="monitor.audit"), TestClient(app) as admin_c, TestClient(app) as anon:
        _login_admin(admin_c)
        r = admin_c.post(f"/users/{bob}/reset", data={"channel": "link"})
        token = re.search(r'/reset/([A-Za-z0-9_\-]+)"', r.text).group(1)
        anon.post(f"/reset/{token}", data={"password": "clave-nueva-12345", "password2": "clave-nueva-12345"})
    lineas = [rec.getMessage() for rec in caplog.records if rec.name == "monitor.audit"]
    assert any("action=reset_link" in m for m in lineas) and any("reset_consumed" in m for m in lineas)
    assert all(token not in m for m in lineas), "el token en claro se logueó"
    assert all(token[:12] not in m for m in lineas)


# ── canal mail ──────────────────────────────────────────────────────────────
@pytest.fixture
def mail_on(monkeypatch):
    """Correo PRENDIDO —SMTP y URL pública, las DOS condiciones de `mail_enabled`— con el
    envío stubeado en los dos routers que mandan mail (el de /forgot corre en background:
    sin el stub abría SMTP real). Devuelve la lista de enviados."""
    from config.settings import settings
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_user", "monitor@test")
    monkeypatch.setattr(settings, "public_url", "http://testserver")
    enviados = []
    def fake_send(to, subject, text, html=None):
        enviados.append({"to": to, "subject": subject, "text": text, "html": html})
    monkeypatch.setattr("apps.web.routers.users_abm.send_mail", fake_send)
    monkeypatch.setattr("apps.web.routers.auth.send_mail", fake_send)
    return enviados


@pytest.mark.noauth
def test_canal_mail_manda_el_link_y_no_lo_muestra(usuarios, mail_on):
    import re
    bob = _bob_id()
    with TestClient(app) as admin_c, TestClient(app) as anon:
        _login_admin(admin_c)
        r = admin_c.post(f"/users/{bob}/reset", data={"channel": "mail"})
        assert r.status_code == 200 and "Link enviado a bob@ejemplo.com" in r.text
        assert 'id="link-reset"' not in r.text, "si el mail salió, el link no se muestra"
        assert len(mail_on) == 1 and mail_on[0]["to"] == "bob@ejemplo.com"
        link = re.search(r"http://testserver/reset/[A-Za-z0-9_\-]+", mail_on[0]["text"]).group(0)
        assert "Bob" in mail_on[0]["text"] and "admin" in mail_on[0]["text"]
        assert anon.get(link.replace("http://testserver", "")).status_code == 200
        assert 'name="password2"' in anon.get(link.replace("http://testserver", "")).text
    with SessionLocal() as s:
        from apps.web import reset_service as rs
        t = rs.tokens_de(s, s.get(UserORM, bob))[0]
        assert (t.channel, t.created_by, t.purpose) == ("mail", "admin", "reset")


@pytest.mark.noauth
def test_canal_mail_si_falla_el_envio_muestra_el_link(usuarios, mail_on, monkeypatch):
    def boom(*a, **k):
        raise OSError("SMTP caído")
    monkeypatch.setattr("apps.web.routers.users_abm.send_mail", boom)
    bob = _bob_id()
    with TestClient(app) as c:
        _login_admin(c)
        r = c.post(f"/users/{bob}/reset", data={"channel": "mail"})
    assert r.status_code == 200 and "No se pudo mandar el mail" in r.text and 'id="link-reset"' in r.text


@pytest.mark.noauth
def test_canal_mail_sin_email_o_sin_smtp_da_400(usuarios, mail_on, monkeypatch):
    """Tres motivos para el 400 (y el botón deshabilitado con el motivo): sin SMTP; con SMTP
    pero sin `MONITOR_PUBLIC_URL` (el correo queda APAGADO: sin URL pública no hay link seguro
    para un mail); y sin email del usuario. En ninguno sale un mail."""
    from config.settings import settings
    bob = _bob_id()
    with TestClient(app) as c:
        _login_admin(c)
        monkeypatch.setattr(settings, "smtp_host", "")
        r = c.post(f"/users/{bob}/reset", data={"channel": "mail"})
        assert r.status_code == 400 and "no está configurado" in r.text
        ficha = c.get(f"/users/{bob}/ficha").text
        assert "Enviar link por mail" in ficha and "disabled" in ficha
        # El motivo dice QUÉ falta (el 400 no se alcanza desde la UI porque el botón está disabled).
        assert 'title="El correo no está configurado en el servidor (MONITOR_SMTP_HOST / MONITOR_PUBLIC_URL)"' in ficha
        monkeypatch.setattr(settings, "smtp_host", "smtp.test")
        monkeypatch.setattr(settings, "public_url", "")
        r = c.post(f"/users/{bob}/reset", data={"channel": "mail"})
        assert r.status_code == 400 and "no está configurado" in r.text
        ficha = c.get(f"/users/{bob}/ficha").text
        assert "Enviar link por mail" in ficha and "disabled" in ficha
        # El motivo dice QUÉ falta (el 400 no se alcanza desde la UI porque el botón está disabled).
        assert 'title="El correo no está configurado en el servidor (MONITOR_SMTP_HOST / MONITOR_PUBLIC_URL)"' in ficha
        monkeypatch.setattr(settings, "public_url", "http://testserver")
        _set_bob(email=None)
        r = c.post(f"/users/{bob}/reset", data={"channel": "mail"})
        assert r.status_code == 400 and "no tiene email" in r.text
    assert mail_on == []


@pytest.mark.noauth
def test_el_boton_enviar_por_mail_se_habilita_solo_con_correo_y_email(usuarios, mail_on):
    """El botón de la ficha es el canal `mail` de `POST /users/{id}/reset`: habilitado con el
    correo prendido y el usuario con email; sin email queda `disabled` con el motivo en el
    `title` (texto exacto del template `fragments/user_ficha.html`)."""
    import re
    bob = _bob_id()

    def boton(html):
        m = re.search(r"<button[^>]*>Enviar link por mail</button>", html)
        assert m, "la ficha no trae el botón «Enviar link por mail»"
        return m.group(0)

    with TestClient(app) as c:
        _login_admin(c)
        b = boton(c.get(f"/users/{bob}/ficha").text)
        assert "disabled" not in b, b
        _set_bob(email=None)
        b = boton(c.get(f"/users/{bob}/ficha").text)
        assert "disabled" in b and 'title="El usuario no tiene email cargado"' in b, b


def _canal_invitacion(username: str) -> str:
    """`channel` persistido del token `invite` vivo de `username` (lo que muestra la Actividad)."""
    from apps.web import reset_service as rs
    with SessionLocal() as s:
        t = rs.tokens_de(s, s.query(UserORM).filter(UserORM.username == username).first())[0]
        assert t.purpose == "invite"
        return t.channel


@pytest.mark.noauth
def test_invitacion_con_email_y_smtp_manda_el_mail(usuarios, mail_on):
    with TestClient(app) as c:
        _login_admin(c)
        r = c.post("/users/add", data={"username": "jperez", "access": "invite", "email": "jperez@ejemplo.com",
                                       "full_name": "Juan Pérez"})
        assert r.status_code == 200 and "se mandó a jperez@ejemplo.com" in r.text and 'id="link-reset"' in r.text
    assert len(mail_on) == 1 and mail_on[0]["to"] == "jperez@ejemplo.com" and "72 horas" in mail_on[0]["text"]
    assert "Te invitaron" in mail_on[0]["subject"]
    assert _canal_invitacion("jperez") == "mail", "se mandó por mail: el canal persistido lo dice"


@pytest.mark.noauth
def test_invitacion_sin_email_no_intenta_mandar(usuarios, mail_on):
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post("/users/add", data={"username": "sinmail", "access": "invite"}).status_code == 200
    assert mail_on == []
    assert _canal_invitacion("sinmail") == "link"


@pytest.mark.noauth
def test_invitacion_si_falla_el_envio_crea_al_usuario_y_muestra_el_link(usuarios, mail_on, monkeypatch):
    """El envío es lo único que puede fallar: el usuario ya existe (invitado, sin clave) y el
    admin ve el error de mail Y el link de respaldo, que abre el formulario."""
    import re
    from core.security import SIN_PASSWORD_HASH

    def boom(*a, **k):
        raise OSError("SMTP caído")
    monkeypatch.setattr("apps.web.routers.users_abm.send_mail", boom)
    with TestClient(app) as admin_c, TestClient(app) as anon:
        _login_admin(admin_c)
        r = admin_c.post("/users/add", data={"username": "jperez", "access": "invite",
                                             "email": "jperez@ejemplo.com"})
        assert r.status_code == 200 and "pero el mail falló" in r.text and 'id="link-reset"' in r.text
        link = re.search(r'value="(http://testserver/reset/[A-Za-z0-9_\-]+)"', r.text).group(1)
        assert 'name="password2"' in anon.get(link.replace("http://testserver", "")).text
    with SessionLocal() as s:
        j = s.query(UserORM).filter(UserORM.username == "jperez").first()
        assert j is not None and j.hashed_password == SIN_PASSWORD_HASH
    assert mail_on == []


# ── /forgot ─────────────────────────────────────────────────────────────────
def _tokens_reset_de_bob() -> int:
    from core.infrastructure.db.models import PasswordResetTokenORM
    with SessionLocal() as s:
        return s.query(PasswordResetTokenORM).filter(
            PasswordResetTokenORM.user_id == _bob_id(), PasswordResetTokenORM.purpose == "reset").count()


@pytest.mark.noauth
def test_forgot_responde_igual_exista_o_no_y_solo_manda_al_valido(usuarios, mail_on):
    """Misma página 200 para: usuario, email (normalizado), inexistente, deshabilitado, sin
    email, INVITADO (sin contraseña elegida: su link lo maneja el admin) y username con otra
    mayúscula (el username se compara exacto; sólo el email se normaliza). Sólo los dos
    primeros mandan mail y emiten token `reset/self`. Mutación: quitar
    `and user.hashed_password != SIN_PASSWORD_HASH` del handler → el invitado recibe mail."""
    import re
    from core.security import SIN_PASSWORD_HASH
    _set_bob(is_active=True)
    with TestClient(app) as c:
        assert "¿Olvidaste tu contraseña?" in c.get("/login").text
        assert 'name="dato"' in c.get("/forgot").text

        def post(dato):
            # los dos límites (IP y dato) se prueban aparte: acá se piden más de 3 "bob"
            auth_router._forgot_attempts_ip.clear(); auth_router._forgot_attempts_dato.clear()
            return c.post("/forgot", data={"dato": dato})

        r_user = post("bob")
        r_mail = post("BOB@ejemplo.com")
        r_nadie = post("nadie@ejemplo.com")
        _set_bob(is_active=False)
        r_off = post("bob")
        _set_bob(is_active=True, email=None)
        r_sinmail = post("bob")
        _set_bob(email="bob@ejemplo.com", hashed_password=SIN_PASSWORD_HASH)
        reset_antes = _tokens_reset_de_bob()
        r_inv = post("bob")
        _set_bob(hashed_password=get_password_hash("bobpass1234"))
        r_Bob = post("Bob")
    respuestas = (r_user, r_mail, r_nadie, r_off, r_sinmail, r_inv, r_Bob)
    assert all(r.status_code == 200 for r in respuestas)
    assert len({r.text for r in respuestas}) == 1, "todas las respuestas tienen que ser IDÉNTICAS"
    assert "Revisá tu correo" in r_user.text
    assert len(mail_on) == 2 and all(m["to"] == "bob@ejemplo.com" for m in mail_on)
    assert _tokens_reset_de_bob() == reset_antes == 2, "ni el invitado ni 'Bob' emiten un token"
    link = re.search(r"http://testserver/reset/[A-Za-z0-9_\-]+", mail_on[-1]["text"]).group(0)
    with TestClient(app) as anon:
        assert 'name="password2"' in anon.get(link.replace("http://testserver", "")).text
    with SessionLocal() as s:
        from apps.web import reset_service as rs
        t = rs.tokens_de(s, s.query(UserORM).filter(UserORM.username == "bob").first())[0]
        assert (t.channel, t.created_by, t.purpose) == ("self", None, "reset")


@pytest.mark.noauth
def test_forgot_con_mail_apagado_no_hace_nada_y_lo_dice(usuarios, monkeypatch):
    from config.settings import settings
    from core.infrastructure.db.models import PasswordResetTokenORM
    monkeypatch.setattr(settings, "smtp_host", "")
    with TestClient(app) as c:
        g = c.get("/forgot")
        assert g.status_code == 200 and "Pedile el link a tu administrador" in g.text and 'name="dato"' not in g.text
        r = c.post("/forgot", data={"dato": "bob"})
        assert r.status_code == 200 and "Revisá tu correo" in r.text
    with SessionLocal() as s:
        assert s.query(PasswordResetTokenORM).count() == 0


@pytest.mark.noauth
def test_forgot_rate_limit_por_ip_y_por_dato(usuarios, mail_on):
    """Dos baldes. Por IP: 3 en 15 min. Por dato: 3 por hora con la clave NORMALIZADA ("bob",
    "BOB" y " bob " son el mismo balde), con el balde de IP limpio antes de cada POST para
    que el 429 lo dé el DATO y no la IP; otro dato desde la misma IP limpia sigue pasando.
    Mutación: `_FORGOT_DATO = (10**6, 3600)` pone el segundo bloque en rojo."""
    with TestClient(app) as c:
        codigos = [c.post("/forgot", data={"dato": f"x{i}@ejemplo.com"}).status_code for i in range(4)]
    assert codigos == [200, 200, 200, 429]
    auth_router._forgot_attempts_ip.clear(); auth_router._forgot_attempts_dato.clear()
    with TestClient(app) as c:
        codigos = []
        for dato in ("bob", "BOB", " bob ", "bob"):
            auth_router._forgot_attempts_ip.clear()
            codigos.append(c.post("/forgot", data={"dato": dato}).status_code)
        assert codigos == [200, 200, 200, 429], "el mismo dato (normalizado) tiene su propio límite: 3 por hora"
        auth_router._forgot_attempts_ip.clear()
        assert c.post("/forgot", data={"dato": "otra@ejemplo.com"}).status_code == 200, "fue el dato, no la IP"


@pytest.mark.noauth
def test_forgot_acota_el_dato_antes_de_usarlo_como_clave_del_balde(usuarios, mail_on):
    """El `maxlength` del form es sólo del cliente: un dato de 5.000 bytes no puede ser la
    clave del balde por dato (`_MAX_TRACKED_KEYS` acota claves, no bytes por clave)."""
    largo = "a" * 5000 + "@ejemplo.com"
    with TestClient(app) as c:
        assert c.post("/forgot", data={"dato": largo}).status_code == 200
    claves = list(auth_router._forgot_attempts_dato)
    assert len(claves) == 1 and len(claves[0]) <= 254, len(claves[0])


@pytest.mark.noauth
def test_forgot_no_arma_el_link_con_el_host_del_request(usuarios, mail_on, monkeypatch):
    """Password-reset poisoning (CWE-640): `POST /forgot` es anónimo y el header `Host` lo
    elige quien lo manda (nginx es catch-all y reenvía `Host $host`). El link del mail sale
    SÓLO de `settings.public_url` (`reset_service.mail_link`), nunca de `request.base_url`.

    Dos escenarios. (1) Con la URL pública seteada, el mail lleva esa base aunque el request
    traiga `Host: evil.example`. (2) Carrera de config —el correo sigue prendido pero
    `public_url` quedó vacía—: fail-closed, NO sale ningún mail (con `reset_link(request, …)`
    saldría uno apuntando a evil.example). Mutación: revertir `/forgot` a
    `reset_service.reset_link(request, token)` pone (2) en rojo."""
    import re
    from config.settings import settings
    with TestClient(app) as c:
        r = c.post("/forgot", data={"dato": "bob"}, headers={"Host": "evil.example"})
    assert r.status_code == 200
    assert len(mail_on) == 1
    assert re.search(r"http://testserver/reset/[A-Za-z0-9_\-]+", mail_on[0]["text"]), mail_on[0]["text"]
    assert "evil.example" not in mail_on[0]["text"] and "evil.example" not in mail_on[0]["html"]
    # (2) el correo quedó "prendido" (se fuerza la propiedad) pero sin URL pública
    monkeypatch.setattr(settings, "public_url", "")
    monkeypatch.setattr(type(settings), "mail_enabled", property(lambda self: True))
    with TestClient(app) as c:
        r2 = c.post("/forgot", data={"dato": "bob"}, headers={"Host": "evil.example"})
        r_nadie = c.post("/forgot", data={"dato": "nadie@ejemplo.com"})
    assert r2.status_code == 200 and r2.text == r_nadie.text
    assert len(mail_on) == 1, "sin URL pública no puede salir NINGÚN mail (y menos uno con el Host del atacante)"
    assert all("evil.example" not in m["text"] and "evil.example" not in (m["html"] or "") for m in mail_on)


@pytest.mark.noauth
def test_forgot_si_falla_el_envio_responde_igual_y_loguea_sin_el_link(usuarios, mail_on, monkeypatch, caplog):
    """El envío corre en background: si falla, la respuesta pública es la MISMA que para un
    dato inexistente y queda un WARNING en `monitor.mail` con el usuario y el tipo de error,
    nunca con el link (el token en claro no va a ningún log)."""
    import logging

    def boom(*a, **k):
        raise OSError("SMTP caído")
    monkeypatch.setattr("apps.web.routers.auth.send_mail", boom)
    with caplog.at_level(logging.WARNING, logger="monitor.mail"), TestClient(app) as c:
        r_bob = c.post("/forgot", data={"dato": "bob"})
        r_nadie = c.post("/forgot", data={"dato": "nadie@ejemplo.com"})
    assert r_bob.status_code == 200 and r_bob.text == r_nadie.text
    avisos = [rec.getMessage() for rec in caplog.records
              if rec.name == "monitor.mail" and rec.levelno >= logging.WARNING]
    assert len(avisos) == 1 and "bob" in avisos[0] and "OSError" in avisos[0], avisos
    assert "/reset/" not in avisos[0]


@pytest.mark.noauth
def test_ni_el_token_ni_el_link_aparecen_en_los_logs_con_el_correo_prendido(usuarios, mail_on, monkeypatch, caplog):
    """Los cinco caminos que mandan (o intentan mandar) un mail —canal mail ok y fallido,
    invitación por mail, /forgot requested y noop— dejan sus líneas de auditoría, y en
    ninguna línea de `monitor.audit` ni de `monitor.mail` aparece el token ni `/reset/`."""
    import logging
    import re
    from apps.web.routers import users_abm
    bob = _bob_id()
    fake = users_abm.send_mail          # el stub de `mail_on`

    def boom(*a, **k):
        raise OSError("SMTP caído")
    with caplog.at_level(logging.INFO), TestClient(app) as admin_c, TestClient(app) as anon:
        _login_admin(admin_c)
        assert admin_c.post(f"/users/{bob}/reset", data={"channel": "mail"}).status_code == 200
        monkeypatch.setattr("apps.web.routers.users_abm.send_mail", boom)
        assert admin_c.post(f"/users/{bob}/reset", data={"channel": "mail"}).status_code == 200
        monkeypatch.setattr("apps.web.routers.users_abm.send_mail", fake)
        assert admin_c.post("/users/add", data={"username": "jperez", "access": "invite",
                                                "email": "jperez@ejemplo.com"}).status_code == 200
        assert anon.post("/forgot", data={"dato": "bob"}).status_code == 200
        assert anon.post("/forgot", data={"dato": "nadie@ejemplo.com"}).status_code == 200
    tokens = [re.search(r"/reset/([A-Za-z0-9_\-]+)", m["text"]).group(1) for m in mail_on]
    assert len(tokens) == 3 and all(len(t) >= 40 for t in tokens)
    lineas = [rec.getMessage() for rec in caplog.records if rec.name in ("monitor.audit", "monitor.mail")]
    assert any("action=reset_link channel=mail" in m and "sent=ok" in m for m in lineas), lineas
    assert any("action=reset_link channel=mail" in m and "sent=fail err=OSError" in m for m in lineas), lineas
    assert any("action=invite_sent" in m and "sent=ok" in m for m in lineas), lineas
    assert any("forgot=requested target=bob" in m for m in lineas), lineas
    assert any("forgot=noop" in m and "target=" not in m for m in lineas), lineas
    for m in lineas:
        assert "/reset/" not in m, m
        for t in tokens:
            assert t not in m and t[:12] not in m, m
