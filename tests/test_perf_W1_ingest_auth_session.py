"""FASE 8 · W1 — UNA resolución de auth por request HTML.

`AuthJinja2Templates.TemplateResponse` abría su PROPIA `SessionLocal()` y volvía a
decodificar el JWT en CADA respuesta de template, ADEMÁS de la que ya había abierto
el `Depends(RequireTabPermission)` / `get_current_user_html`. Son 2 sesiones SQLite
+ 2 decodes por fragmento, y el dashboard pide ~14 fragmentos cada 5s por cliente.

Los deps de `deps_auth` publican el usuario resuelto en `request.state.current_user`;
`TemplateResponse` lo lee de ahí. El camino viejo queda de FALLBACK para las
respuestas sin dependencia de auth (`/login`) — y es el que sigue usando la fixture
`_auth_bypass` de conftest, cuyos overrides no reciben el `request`.
"""

import pytest
from fastapi.testclient import TestClient

from apps.web import deps_auth, templates as tpl_mod
from apps.web.app import app
from apps.web.routers import auth as auth_router
from core.infrastructure.db.engine import SessionLocal, get_engine
from core.infrastructure.db.models import Base, UserORM
from core.security import get_password_hash

pytestmark = pytest.mark.noauth   # auth REAL: sin el bypass de conftest


@pytest.fixture
def admin():
    Base.metadata.create_all(bind=get_engine())
    auth_router._login_attempts.clear()
    with SessionLocal() as s:
        s.query(UserORM).delete()
        s.add(UserORM(username="admin", hashed_password=get_password_hash("adminpass"),
                      is_admin=True, allowed_tabs=["*"]))
        s.add(UserORM(username="bob", hashed_password=get_password_hash("bobpass"),
                      is_admin=False, allowed_tabs=["bonos", "cashflows"]))
        s.commit()
    yield
    with SessionLocal() as s:
        s.query(UserORM).delete()
        s.commit()
    auth_router._login_attempts.clear()


@pytest.fixture
def counters(monkeypatch):
    """Cuenta SessionLocal() y decode_access_token() en los dos módulos de auth."""
    n = {"sessions": 0, "decodes": 0}

    def counting_session(*a, **kw):
        n["sessions"] += 1
        return SessionLocal(*a, **kw)

    real_decode = deps_auth.decode_access_token

    def counting_decode(tok):
        n["decodes"] += 1
        return real_decode(tok)

    monkeypatch.setattr(deps_auth, "SessionLocal", counting_session)
    monkeypatch.setattr(tpl_mod, "SessionLocal", counting_session)
    monkeypatch.setattr(deps_auth, "decode_access_token", counting_decode)
    return n


def _login(c: TestClient):
    r = c.post("/login", data={"username": "admin", "password": "adminpass"},
               follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code


def test_un_solo_session_y_decode_por_fragmento_html(admin, counters):
    with TestClient(app) as c:
        _login(c)
        counters["sessions"] = 0
        counters["decodes"] = 0
        r = c.get("/health/badge")
    assert r.status_code == 200
    assert "<" in r.text                       # es un fragmento renderizado
    assert counters["sessions"] == 1, (
        f"{counters['sessions']} SessionLocal() por request HTML (esperado 1)")
    assert counters["decodes"] == 1, (
        f"{counters['decodes']} decodes de JWT por request HTML (esperado 1)")


def test_el_contexto_del_template_sigue_trayendo_el_usuario(admin):
    """El nav se arma con `current_user` / `has_tab`: la optimización no puede
    dejarlos vacíos."""
    with TestClient(app) as c:
        _login(c)
        r = c.get("/cashflows")
    assert r.status_code == 200
    assert "admin" in r.text


def test_login_sin_cookie_no_rompe_el_fallback(admin, counters):
    """`/login` no tiene dependencia de auth → `request.state.current_user` no
    existe y el template debe caer al camino viejo sin explotar."""
    with TestClient(app) as c:
        r = c.get("/login")
    assert r.status_code == 200
    assert counters["sessions"] >= 1        # fallback: abre su propia sesión


def test_has_tab_por_request_state_respeta_los_permisos(admin):
    """El `has_tab()` del contexto pasó a resolverse desde `request.state`: un NO
    admin tiene que seguir viendo SOLO su nav (la regla vive en `user_can_tab`)."""
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "bob", "password": "bobpass"},
                   follow_redirects=False)
        assert r.status_code in (302, 303)
        r = c.get("/cashflows")
    assert r.status_code == 200
    assert '<a href="/cashflows">Cashflows</a>' in r.text
    assert '<a href="/">Bonos</a>' in r.text
    assert '<a href="/bcra">BCRA</a>' not in r.text
    assert '<a href="/abm">ABM Bonos</a>' not in r.text
