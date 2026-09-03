"""Auditoría 2026-09 — lote D1 (seguridad de la capa web).

Cada test reproduce un hallazgo concreto: XSS reflejado del 404 de /bond, bypass del
rate-limit de /login vía X-Forwarded-For, XSS almacenado del panel de usuarios,
docs de OpenAPI sin login, 500 de la ABM de usuarios con un id inexistente y el
encierro del usuario sin la pestaña 'bonos'.
"""

import asyncio
import time
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from apps.web.app import app


# ── Hallazgo 1/3: XSS reflejado en el 404 de /bond/{ticker}/detail ──────────
def test_bond_detail_404_escapa_el_ticker():
    payload = "<img src=x onerror=alert(1)>"
    with TestClient(app) as c:
        r = c.get(f"/bond/{payload}/detail")
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "<img" not in r.text, "el ticker se refleja como HTML vivo (XSS)"
    assert "&lt;img" in r.text, "el ticker debería aparecer escapado"


# ── Hallazgo 4: POST /bond/{ticker}/cer convierte el plazo T+0 en T+1 ───────
def test_cer_drawer_post_respeta_lag_cero(monkeypatch):
    from apps.web.routers import bonds as bonds_router

    visto = {}

    def _fake_projection(ticker, repo, provider, indices, fx, **kw):
        visto["lag"] = kw.get("settlement_lag")
        return {"rows": [], "scenarios": [], "months": [], "default_unif": None}

    monkeypatch.setattr(bonds_router, "cer_projection", _fake_projection)
    with TestClient(app) as c:
        c.post("/bond/TZXD7/cer", data={"lag": "0", "mode": "uniforme", "price": "100"})
    assert visto["lag"] == 0, "el POST pisó el T+0 con T+1"

    with TestClient(app) as c:
        c.post("/bond/TZXD7/cer", data={"mode": "uniforme", "price": "100"})
    assert visto["lag"] == 1, "sin `lag` en el form el default sigue siendo T+1"


# ── Hallazgo 2: el rate-limit de /login se saltea con X-Forwarded-For ───────
def _limpiar_intentos():
    from apps.web.routers import auth as auth_router
    auth_router._login_attempts.clear()


def test_login_rate_limit_no_se_saltea_con_x_forwarded_for():
    """Un peer NO confiable no puede elegir su propio bucket con el header."""
    from apps.web.routers import auth as auth_router

    _limpiar_intentos()
    try:
        codes = []
        with TestClient(app) as c:
            for i in range(12):
                r = c.post("/login",
                           data={"username": "admin", "password": f"mala{i}"},
                           headers={"X-Forwarded-For": f"1.2.3.{i}, 10.0.0.1"})
                codes.append(r.status_code)
        assert 429 in codes, f"el limiter nunca disparó: {codes}"
        assert len(auth_router._login_attempts) == 1, (
            f"el header abrió {len(auth_router._login_attempts)} buckets distintos")
    finally:
        _limpiar_intentos()


def test_client_ip_usa_el_ultimo_xff_solo_desde_un_proxy_confiable():
    from apps.web.routers import auth as auth_router

    _limpiar_intentos()
    try:
        with TestClient(app, client=("127.0.0.1", 4242)) as c:
            c.post("/login", data={"username": "admin", "password": "mala"},
                   headers={"X-Forwarded-For": "9.9.9.9, 203.0.113.7"})
        keys = list(auth_router._login_attempts)
        assert keys == [("203.0.113.7", "admin")], keys
    finally:
        _limpiar_intentos()


def test_login_attempts_se_poda_globalmente():
    """Sin barrido global el dict crece sin techo (una entrada por intento fallido)."""
    from apps.web.routers import auth as auth_router

    _limpiar_intentos()
    try:
        viejo = time.time() - auth_router._LOGIN_WINDOW_SEC - 1
        for i in range(50):
            auth_router._login_attempts[(f"10.0.0.{i}", "admin")] = [viejo]
        with TestClient(app) as c:
            c.post("/login", data={"username": "admin", "password": "mala"})
        assert len(auth_router._login_attempts) <= 2, (
            f"quedaron {len(auth_router._login_attempts)} claves vencidas vivas")
    finally:
        _limpiar_intentos()


# ── Hallazgo 5: /users/{reset-password,update}/{id} tiran 500 si el id no existe ──
def _crear_tablas_users():
    from core.infrastructure.db.engine import get_engine
    from core.infrastructure.db.models import Base
    Base.metadata.create_all(bind=get_engine())


def test_users_abm_id_inexistente_no_tira_500():
    _crear_tablas_users()
    with TestClient(app, raise_server_exceptions=False) as c:
        r1 = c.post("/users/reset-password/999999", data={"password": "x"})
        r2 = c.post("/users/update/999999", data={"is_admin": "false"})
        r3 = c.post("/users/delete/999999")
    assert r1.status_code == 404, f"reset-password devolvió {r1.status_code}"
    assert r2.status_code == 404, f"update devolvió {r2.status_code}"
    assert r3.status_code == 404, f"delete devolvió {r3.status_code}"
    assert "borrado" not in r3.text.lower(), "delete miente: dice que borró un id inexistente"


# ── Hallazgo 6: XSS almacenado — username dentro de onclick/onsubmit ────────
class _ManejadoresInline(HTMLParser):
    """Junta el valor DECODIFICADO de los atributos de evento (on*), que es
    exactamente el texto que el motor JS compila (el tokenizador HTML resuelve
    las entidades ANTES de entregárselo)."""

    def __init__(self):
        super().__init__()
        self.handlers = []
        self.attr_names = set()

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            self.attr_names.add(name)
            if name.startswith("on") and value:
                self.handlers.append(value)


def test_users_page_no_mete_el_username_en_atributos_de_evento():
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import UserORM

    _crear_tablas_users()
    # (1) rompe el literal JS del handler; (2) rompe el atributo con comilla doble.
    payloads = ["zz'),alert(1),('", 'zz" onmouseover="alert(2)']
    with SessionLocal() as s:
        s.query(UserORM).filter(UserORM.username.in_(payloads)).delete(
            synchronize_session=False)
        for i, pl in enumerate(payloads):
            s.add(UserORM(username=pl, hashed_password="x",
                          is_admin=False, allowed_tabs=["fci"]))
        s.commit()
    try:
        with TestClient(app) as c:
            r = c.get("/users")
        assert r.status_code == 200
        parser = _ManejadoresInline()
        parser.feed(r.text)
        culpables = [h for h in parser.handlers
                     if "alert(1)" in h or "alert(2)" in h]
        assert not culpables, f"el username rompe el literal JS del handler: {culpables}"
        assert "onmouseover" not in parser.attr_names, (
            "el username se escapó del atributo y creó un handler nuevo")
    finally:
        with SessionLocal() as s:
            s.query(UserORM).filter(UserORM.username.in_(payloads)).delete(
                synchronize_session=False)
            s.commit()


# ── Hallazgo 7: /docs, /redoc y /openapi.json públicos sin login ────────────
@pytest.mark.noauth
def test_openapi_y_docs_no_son_publicos():
    with TestClient(app) as c:
        for path in ("/openapi.json", "/docs", "/redoc"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code != 200, f"{path} se sirve sin login ({len(r.content)} bytes)"


# ── Hallazgo 9: el usuario sin la pestaña 'bonos' queda encerrado ───────────
@pytest.fixture
def usuarios_landing():
    from apps.web.routers import auth as auth_router
    from core.infrastructure.db.engine import SessionLocal, get_engine
    from core.infrastructure.db.models import Base, UserORM
    from core.security import get_password_hash

    Base.metadata.create_all(bind=get_engine())
    auth_router._login_attempts.clear()
    nombres = ("aud_contador", "aud_bonista", "aud_sinacceso")
    with SessionLocal() as s:
        s.query(UserORM).filter(UserORM.username.in_(nombres)).delete(
            synchronize_session=False)
        s.add(UserORM(username="aud_contador", hashed_password=get_password_hash("pw"),
                      is_admin=False, allowed_tabs=["fci", "cartera"]))
        s.add(UserORM(username="aud_bonista", hashed_password=get_password_hash("pw"),
                      is_admin=False, allowed_tabs=["bonos", "fci"]))
        s.add(UserORM(username="aud_sinacceso", hashed_password=get_password_hash("pw"),
                      is_admin=False, allowed_tabs=[]))
        s.commit()
    yield
    with SessionLocal() as s:
        s.query(UserORM).filter(UserORM.username.in_(nombres)).delete(
            synchronize_session=False)
        s.commit()
    auth_router._login_attempts.clear()


@pytest.mark.noauth
def test_login_manda_a_la_primera_pestana_permitida(usuarios_landing):
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "aud_contador", "password": "pw"},
                   follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/cartera", r.headers["location"]
        # y el destino es realmente alcanzable (no rebota al login)
        assert c.get("/cartera", follow_redirects=False).status_code == 200


@pytest.mark.noauth
def test_login_con_bonos_sigue_yendo_al_home(usuarios_landing):
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "aud_bonista", "password": "pw"},
                   follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"


@pytest.mark.noauth
def test_login_sin_ninguna_pestana_avisa_en_vez_de_rebotar(usuarios_landing):
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "aud_sinacceso", "password": "pw"},
                   follow_redirects=False)
    assert r.status_code != 302, "lo manda a una home que rebota al login (loop)"
    assert "módulo" in r.text or "modulo" in r.text


# ── Hallazgo 8: el wiring de los loops del lifespan no estaba aserteado ─────
@pytest.mark.noauth
def test_lifespan_arranca_los_cinco_loops_bajo_supervisor(monkeypatch):
    """Red de seguridad del wiring: los 5 loops tienen que existir Y estar envueltos
    en `supervise` (un loop suelto que muere no se reinicia — incidente 2026-09-01)."""
    import apps.web.app as app_mod

    monkeypatch.delenv("MONITOR_DISABLE_LOOPS", raising=False)
    corridas = {"refresh": 0}

    async def _noop(app):
        return None

    async def _dormido(app):
        await asyncio.sleep(3600)

    async def _muere(app):
        corridas["refresh"] += 1
        return None          # termina sola → el supervisor debe reiniciarla

    monkeypatch.setattr(app_mod, "_startup_reconcile", _noop)
    monkeypatch.setattr(app_mod, "_refresh_loop", _muere)
    for nombre in ("_options_loop", "_bei_loop", "_price_history_loop", "_ratings_loop"):
        monkeypatch.setattr(app_mod, nombre, _dormido)

    visto = {}

    async def _correr():
        async with app_mod.lifespan(app_mod.app):
            visto["tasks"] = {t.get_name() for t in asyncio.all_tasks()}
            await asyncio.sleep(1.4)   # base_delay del supervisor = 1s
            visto["last_error"] = app_mod.app.state.app_state.last_error

    asyncio.run(_correr())

    esperados = {"loop:refresh", "loop:options", "loop:bei",
                 "loop:price_history", "loop:ratings"}
    assert esperados <= visto["tasks"], esperados - visto["tasks"]
    assert corridas["refresh"] >= 2, "el loop caído no se reinició (¿sin supervise?)"
    assert visto["last_error"] and "refresh" in visto["last_error"], visto["last_error"]
