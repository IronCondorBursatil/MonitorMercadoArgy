"""Auditoría de autenticación/administración y reglas de contraseña.

Antes de esto, en un servidor público: un login exitoso, uno fallido, un alta de
usuario, un borrado o un reset de contraseña **no dejaban rastro en ningún lado**.
`routers/auth.py` y `routers/users_abm.py` no importaban `logging`, el handler de
archivo es WARNING+ y el filtro de consola descarta los access 2xx/3xx de uvicorn.
Sin registro no hay forma de saber si alguien entró.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from apps.web.app import app
from apps.web.routers import auth as auth_router
from core.infrastructure.db.engine import SessionLocal, get_engine
from core.infrastructure.db.models import Base, UserORM
from core.security import get_password_hash

pytestmark = pytest.mark.noauth


@pytest.fixture
def usuarios():
    Base.metadata.create_all(bind=get_engine())
    auth_router._login_attempts.clear()
    with SessionLocal() as s:
        s.query(UserORM).delete()
        s.add(UserORM(username="admin", hashed_password=get_password_hash("adminpass1"),
                      is_admin=True, allowed_tabs=["*"]))
        s.add(UserORM(username="bob", hashed_password=get_password_hash("bobpass1234"),
                      is_admin=False, allowed_tabs=["bonos"]))
        s.commit()
    yield
    with SessionLocal() as s:
        s.query(UserORM).delete()
        s.commit()
    auth_router._login_attempts.clear()


def _lineas(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "monitor.audit"]


# ── auth ───────────────────────────────────────────────────────────────────
def test_un_login_fallido_queda_registrado(usuarios, caplog):
    with caplog.at_level(logging.INFO, logger="monitor.audit"), TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "mal"},
               follow_redirects=False)
    assert any("login=fail" in m and "user=admin" in m and "ip=" in m
               for m in _lineas(caplog)), _lineas(caplog)


def test_un_login_exitoso_tambien(usuarios, caplog):
    with caplog.at_level(logging.INFO, logger="monitor.audit"), TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "adminpass1"},
               follow_redirects=False)
    assert any("login=ok" in m and "user=admin" in m for m in _lineas(caplog))


def test_el_registro_llega_al_journal(usuarios, caplog):
    """La app manda a stdout sólo WARNING+ … salvo lo que viene marcado con
    `console=True`. Sin esa marca el registro existe pero NO sale del proceso: no
    llega a journald y nadie lo ve nunca."""
    from config.settings import _ConsoleFilter

    with caplog.at_level(logging.INFO, logger="monitor.audit"), TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "mal"},
               follow_redirects=False)
    audit = [r for r in caplog.records if r.name == "monitor.audit"]
    assert audit, "no se emitió ningún registro"
    for r in audit:
        assert getattr(r, "console", False) is True, "sin console=True no sale a journald"
        assert _ConsoleFilter().filter(r), "el filtro de consola lo descarta"


def test_un_usuario_con_salto_de_linea_no_parte_el_registro(usuarios, caplog):
    """Log injection: con un `\\n` en el nombre, el atacante escribe una línea de
    auditoría inventada (por ejemplo un `login=ok` que nunca pasó)."""
    with caplog.at_level(logging.INFO, logger="monitor.audit"), TestClient(app) as c:
        c.post("/login", data={"username": "x\nauth login=ok user=admin", "password": "y"},
               follow_redirects=False)
    for m in _lineas(caplog):
        assert "\n" not in m, f"registro multilínea: {m!r}"


# ── administración ─────────────────────────────────────────────────────────
def _login_admin(c):
    r = c.post("/login", data={"username": "admin", "password": "adminpass1"},
               follow_redirects=False)
    assert r.status_code in (302, 303)


def test_las_acciones_de_admin_dicen_quien_le_hizo_que_a_quien(usuarios, caplog):
    with TestClient(app) as c:
        _login_admin(c)
        with SessionLocal() as s:
            bob = s.query(UserORM).filter(UserORM.username == "bob").first().id
        with caplog.at_level(logging.INFO, logger="monitor.audit"):
            c.post(f"/users/reset-password/{bob}", data={"password": "otraclave12"})
    assert any("action=reset_password" in m and "by=" in m and "target=bob" in m
               for m in _lineas(caplog)), _lineas(caplog)


def test_el_borrado_registra_a_quien_se_borro(usuarios, caplog):
    with TestClient(app) as c:
        _login_admin(c)
        with SessionLocal() as s:
            bob = s.query(UserORM).filter(UserORM.username == "bob").first().id
        with caplog.at_level(logging.INFO, logger="monitor.audit"):
            c.post(f"/users/delete/{bob}")
    assert any("action=delete" in m and "target=bob" in m for m in _lineas(caplog))


# ── contraseñas ────────────────────────────────────────────────────────────
def test_no_deja_crear_un_usuario_con_password_corta(usuarios):
    with TestClient(app) as c:
        _login_admin(c)
        r = c.post("/users/add", data={"username": "nuevo", "password": "corta"})
    assert r.status_code == 400
    with SessionLocal() as s:
        assert s.query(UserORM).filter(UserORM.username == "nuevo").first() is None


def test_rechaza_lo_que_bcrypt_truncaria(usuarios):
    """bcrypt ignora todo lo que pase de 72 bytes, y passlib lo hace EN SILENCIO: una
    contraseña de 200 caracteres y sus primeros 72 bytes son la misma."""
    with TestClient(app) as c:
        _login_admin(c)
        r = c.post("/users/add", data={"username": "largo", "password": "a" * 80})
    assert r.status_code == 400
    with SessionLocal() as s:
        assert s.query(UserORM).filter(UserORM.username == "largo").first() is None


def test_el_reset_valida_pero_DESPUES_del_lookup(usuarios):
    """Un id inexistente da 404 aunque la contraseña también sea inválida — lo fija
    `test_aud_D1_seguridad_web`. Validar antes convertiría ese 404 en un 400."""
    with TestClient(app) as c:
        _login_admin(c)
        assert c.post("/users/reset-password/999999", data={"password": "x"}).status_code == 404
        with SessionLocal() as s:
            bob = s.query(UserORM).filter(UserORM.username == "bob").first()
            hash_antes = bob.hashed_password
            bob_id = bob.id
        assert c.post(f"/users/reset-password/{bob_id}", data={"password": "x"}).status_code == 400
    with SessionLocal() as s:
        assert s.get(UserORM, bob_id).hashed_password == hash_antes


def test_una_password_valida_sigue_andando(usuarios):
    with TestClient(app) as c:
        _login_admin(c)
        r = c.post("/users/add", data={"username": "valido", "password": "claveLarga123"})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.query(UserORM).filter(UserORM.username == "valido").first() is not None


# ── superficie de sesión ───────────────────────────────────────────────────
def test_ya_no_se_entra_por_el_header_Authorization(usuarios):
    """Había un fallback `Authorization: Bearer` sin un solo consumidor en el repo:
    una segunda puerta a la sesión que nadie usaba y ningún test cubría."""
    from core.security import create_access_token

    token = create_access_token({"sub": "admin"})
    with TestClient(app) as c:
        r = c.get("/", headers={"Authorization": f"Bearer {token}"},
                  follow_redirects=False)
    assert r.status_code == 302, "el header sigue abriendo sesión"
