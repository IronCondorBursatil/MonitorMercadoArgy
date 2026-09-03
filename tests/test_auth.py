"""Auth real (login, JWT, permisos por pestaña, rate-limit). Corre SIN el bypass de
auth de conftest (@pytest.mark.noauth) → ejercita el camino completo con usuarios en
la DB de test. Cubre las ~800 líneas de seguridad que entraron sin tests."""

import datetime

import jwt
import pytest
from fastapi.testclient import TestClient

from apps.web.app import app
from apps.web.routers import auth as auth_router
from config.settings import settings
from core.infrastructure.db.engine import SessionLocal, get_engine
from core.infrastructure.db.models import Base, UserORM
from core.security import (
    create_access_token, decode_access_token, get_password_hash, verify_password,
)

pytestmark = pytest.mark.noauth   # todo el módulo: auth REAL, sin bypass


@pytest.fixture
def users():
    """Admin + usuario limitado (solo pestaña 'bonos') en la DB de test."""
    Base.metadata.create_all(bind=get_engine())
    auth_router._login_attempts.clear()   # limiter en memoria: aislar entre tests
    with SessionLocal() as s:
        s.query(UserORM).delete()
        s.add(UserORM(username="admin", hashed_password=get_password_hash("adminpass"),
                      is_admin=True, allowed_tabs=["*"]))
        s.add(UserORM(username="bob", hashed_password=get_password_hash("bobpass"),
                      is_admin=False, allowed_tabs=["bonos"]))
        s.commit()
    yield
    with SessionLocal() as s:
        s.query(UserORM).delete()
        s.commit()
    auth_router._login_attempts.clear()


# ── core/security.py ────────────────────────────────────────────────────────
def test_password_hash_roundtrip():
    h = get_password_hash("secret")
    assert verify_password("secret", h)
    assert not verify_password("wrong", h)


def test_token_roundtrip():
    tok = create_access_token({"sub": "alice"})
    assert decode_access_token(tok)["sub"] == "alice"


def test_expired_token_rejected():
    tok = create_access_token({"sub": "alice"},
                              expires_delta=datetime.timedelta(seconds=-1))
    assert decode_access_token(tok) is None


def test_token_signed_with_other_key_rejected():
    forged = jwt.encode({"sub": "admin"}, "otra-clave-cualquiera",
                        algorithm=settings.jwt_algorithm)
    assert decode_access_token(forged) is None


def test_secret_is_not_the_old_hardcoded_default():
    # El secreto publicado en el repo NO debe seguir firmando tokens válidos.
    assert settings.jwt_secret_key != "super_secreto_para_desarrollo_cambiar_en_prod"
    assert settings.jwt_secret_key


# ── router de login ─────────────────────────────────────────────────────────
def test_root_without_cookie_redirects_to_login(users):
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_login_bad_password_sets_no_cookie(users):
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "admin", "password": "mala"},
                   follow_redirects=False)
    assert "access_token" not in r.cookies
    assert "incorrectos" in r.text.lower()


def test_login_ok_sets_httponly_cookie(users):
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "admin", "password": "adminpass"},
                   follow_redirects=False)
    assert r.status_code == 302
    set_cookie = r.headers.get("set-cookie", "").lower()
    assert "access_token=" in set_cookie and "httponly" in set_cookie


def test_logout_is_post_not_get(users):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "adminpass"})
        assert c.get("/logout", follow_redirects=False).status_code == 405   # GET no existe
        assert c.post("/logout", follow_redirects=False).status_code == 302


def test_login_rate_limited_after_5_failures(users):
    with TestClient(app) as c:
        for _ in range(5):
            c.post("/login", data={"username": "admin", "password": "mala"})
        r = c.post("/login", data={"username": "admin", "password": "mala"})
    assert r.status_code == 429


# ── permisos por pestaña ────────────────────────────────────────────────────
def test_limited_user_reaches_allowed_tab(users):
    with TestClient(app) as c:
        c.post("/login", data={"username": "bob", "password": "bobpass"})
        r = c.get("/", follow_redirects=False)     # 'bonos' permitido
    assert r.status_code == 200


def test_limited_user_blocked_from_other_tab(users):
    """403 'sin permiso', NO 302 al login: el usuario está autenticado — mandarlo al
    formulario le decía 'sesión vencida' y lo dejaba reintentando la clave (ver
    apps/web/deps_auth.TabForbiddenException y tests/test_rem_R3_web_seguridad.py)."""
    with TestClient(app) as c:
        c.post("/login", data={"username": "bob", "password": "bobpass"})
        r = c.get("/fci", follow_redirects=False)  # 'fci' NO permitido
    assert r.status_code == 403
    assert "location" not in r.headers, r.headers


def test_nonadmin_denied_users_page(users):
    with TestClient(app) as c:
        c.post("/login", data={"username": "bob", "password": "bobpass"})
        r = c.get("/users", follow_redirects=False)
    assert r.status_code == 403


def test_admin_reaches_users_page(users):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "adminpass"})
        r = c.get("/users", follow_redirects=False)
    assert r.status_code == 200
