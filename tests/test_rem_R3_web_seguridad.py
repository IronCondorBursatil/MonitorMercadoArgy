"""Remediación R3_web (lote D1) — los tres pendientes de seguridad/robustez que la
auditoría dejó abiertos porque los archivos no eran de ningún lote:

  1. `routers/abm.py` interpolaba `str(e)` sin escapar en un HTML armado a mano
     (mismo XSS reflejado que ya se arregló en el 404 de /bond/{ticker}/detail).
  2. `deps_auth.RequireTabPermission` levantaba `RequiresLoginException` → 302 a
     /login: al usuario logueado que teclea una URL sin permiso le parecía una
     sesión vencida. Falta de PERMISO ≠ falta de LOGIN → 403.
  3. `routers/bonds.py` tomaba `lag: int = 1` sin cota: `?lag=5` reventaba
     `settlement_byma_date` con un 500 (mismo defecto de clase que el `?days=` de
     /cashflows).

Y la convención de config: `MONITOR_TRUSTED_PROXY_IPS` se leía con `os.environ` en
vez de declararse en `config/settings.py` (el repo lee el env SOLO por pydantic).
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from apps.web.app import app


# ── 1. XSS reflejado en el flash de /abm/cashflows ──────────────────────────
class _Etiquetas(HTMLParser):
    """Tags que el navegador construye de verdad (el parser resuelve entidades)."""

    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


def test_abm_cashflows_escapa_el_mensaje_de_error(monkeypatch):
    from apps.web import instruments_abm as abm_store

    payload = "<img src=x onerror=alert(1)>"

    def _boom(ticker, cfs):
        raise ValueError(f"ticker desconocido: {payload}")

    monkeypatch.setattr(abm_store, "save_cashflows", _boom)
    with TestClient(app) as c:
        r = c.post("/abm/cashflows", data={"cf_ticker": "ZZZZ", "cf_date": "2027-01-01",
                                           "cf_amort": "100", "cf_interest": "0"})
    assert r.status_code == 200
    parser = _Etiquetas()
    parser.feed(r.text)
    assert "img" not in parser.tags, "el mensaje de excepción se refleja como HTML vivo"
    assert "&lt;img" in r.text, "debería aparecer escapado"


# ── 2. Sin permiso = 403, no un rebote al login ─────────────────────────────
@pytest.fixture
def usuario_sin_bonos():
    """Usuario logueado con la pestaña `fci` (y NO `bonos`)."""
    from apps.web.routers import auth as auth_router
    from core.infrastructure.db.engine import SessionLocal, get_engine
    from core.infrastructure.db.models import Base, UserORM
    from core.security import get_password_hash

    Base.metadata.create_all(bind=get_engine())
    auth_router._login_attempts.clear()
    nombre = "rem_sin_bonos"
    with SessionLocal() as s:
        s.query(UserORM).filter(UserORM.username == nombre).delete(
            synchronize_session=False)
        s.add(UserORM(username=nombre, hashed_password=get_password_hash("pw"),
                      is_admin=False, allowed_tabs=["fci"]))
        s.commit()
    yield nombre
    with SessionLocal() as s:
        s.query(UserORM).filter(UserORM.username == nombre).delete(
            synchronize_session=False)
        s.commit()
    auth_router._login_attempts.clear()


@pytest.mark.noauth
def test_una_pestana_sin_permiso_da_403_y_no_manda_al_login(usuario_sin_bonos):
    with TestClient(app) as c:
        r = c.post("/login", data={"username": usuario_sin_bonos, "password": "pw"},
                   follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/fci"
        # ya logueado, teclea a mano una URL de otra pestaña
        r = c.get("/", follow_redirects=False)
    assert r.status_code == 403, (
        f"falta de permiso devuelve {r.status_code} (302 al login = 'sesión vencida' "
        "para alguien que está logueado)")
    assert "login" not in r.headers.get("location", ""), r.headers
    assert "Sin permiso" in r.text
    assert "/fci" in r.text, "no le dice a dónde SÍ puede ir"


@pytest.mark.noauth
def test_sin_cookie_sigue_yendo_al_login():
    """La contracara: NO autenticado sí es un redirect al formulario."""
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"


@pytest.mark.noauth
def test_un_fragmento_htmx_sin_login_devuelve_el_hx_redirect():
    """Bug encontrado al cablear el 403: el handler armaba
    `JSONResponse(status_code=200, headers=...)` sin `content` —que es posicional y
    obligatorio— así que el camino HTMX del deslogueado tiraba TypeError → 500 SIN
    `HX-Redirect`, y el panel quedaba mudo en vez de volver al login."""
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/panels/bonares/rows", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.status_code
    assert r.headers.get("HX-Redirect") == "/login", dict(r.headers)


@pytest.mark.noauth
def test_htmx_sin_permiso_no_devuelve_el_redirect_de_login(usuario_sin_bonos):
    """Un fragmento HTMX de una pestaña sin permiso no puede contestar 200 con el
    `HX-Redirect: /login` (el front lo trataba como sesión caída y deslogueaba)."""
    with TestClient(app) as c:
        c.post("/login", data={"username": usuario_sin_bonos, "password": "pw"},
               follow_redirects=False)
        r = c.get("/panels/bonares/rows", headers={"HX-Request": "true"},
                  follow_redirects=False)
    assert r.status_code == 403, r.status_code
    assert "HX-Redirect" not in r.headers, r.headers


# ── 3. `lag` acotado a T+0/T+1 (BYMA no tiene otro plazo) ───────────────────
@pytest.mark.parametrize("lag", [5, -1, 2, 99])
def test_bond_detail_rechaza_un_lag_fuera_de_rango(lag):
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get(f"/bond/AL30/detail?lag={lag}")
    assert r.status_code == 422, (
        f"lag={lag} devolvió {r.status_code}: `settlement_byma_date` sólo acepta "
        "T+0/T+1, sin cota es un 500")


@pytest.mark.parametrize("lag", [0, 1])
def test_bond_detail_acepta_los_dos_plazos_reales(lag):
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get(f"/bond/AL30/detail?lag={lag}")
    assert r.status_code in (200, 404), r.status_code   # 404 = no está en el catálogo


def test_el_cajon_cer_tambien_acota_el_lag():
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/bond/TZXD7/cer?lag=7").status_code == 422


def test_el_post_del_cajon_cer_no_revienta_con_un_lag_absurdo(monkeypatch):
    """El POST parsea el form a mano (no hay `Query`): se clampea igual."""
    from apps.web.routers import bonds as bonds_router

    visto = {}

    def _fake_projection(ticker, repo, provider, indices, fx, **kw):
        visto["lag"] = kw.get("settlement_lag")
        return {"rows": [], "scenarios": [], "months": [], "default_unif": None}

    monkeypatch.setattr(bonds_router, "cer_projection", _fake_projection)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/bond/TZXD7/cer", data={"lag": "9", "mode": "uniforme"})
    assert r.status_code == 200, r.status_code
    assert visto["lag"] in (0, 1), visto


@pytest.mark.parametrize("crudo", ["nan", "inf", "-inf", "1e400"])
def test_el_post_del_cajon_cer_tampoco_revienta_con_un_lag_no_finito(monkeypatch, crudo):
    """Re-auditoría: `_to_float` acepta 'nan'/'inf'/'1e400' —`float()` los parsea— y
    `int()` sobre esos levanta ValueError/OverflowError, así que el clamp entero
    dejaba abierto el MISMO 500 por la puerta de al lado."""
    from apps.web.routers import bonds as bonds_router

    visto = {}

    def _fake_projection(ticker, repo, provider, indices, fx, **kw):
        visto["lag"] = kw.get("settlement_lag")
        return {"rows": [], "scenarios": [], "months": [], "default_unif": None}

    monkeypatch.setattr(bonds_router, "cer_projection", _fake_projection)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/bond/TZXD7/cer", data={"lag": crudo, "mode": "uniforme"})
    assert r.status_code == 200, f"lag={crudo!r} devolvió {r.status_code}"
    assert visto["lag"] in (0, 1), visto


def test_la_calculadora_rechaza_un_settlement_lag_invalido():
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/bond/AL30/metrics", data={"settlement_lag": "4", "price": "70"})
    assert r.status_code == 422, r.status_code


# ── 4. Convención de config: el env lo lee pydantic, no os.environ ──────────
def test_los_proxies_confiables_son_un_campo_de_settings():
    from config.settings import Settings, settings

    assert "trusted_proxy_ips" in Settings.model_fields, \
        "el campo no está declarado en config/settings.py (convención del repo)"
    assert settings.trusted_proxy_ips == "127.0.0.1,::1"


def test_auth_no_lee_la_env_por_su_cuenta(monkeypatch):
    """`_trusted_proxies()` tiene que salir SOLO de settings: con las dos fuentes, el
    `getattr(settings, ...)` ganaba en silencio sobre el env (precedencia invertida)."""
    import apps.web.routers.auth as auth_router

    import ast

    with open(auth_router.__file__, encoding="utf-8") as f:
        arbol = ast.parse(f.read())
    lecturas = [n for n in ast.walk(arbol)
                if (isinstance(n, ast.Attribute) and n.attr in ("environ", "getenv"))]
    assert not lecturas and not hasattr(auth_router, "os"), \
        "auth.py sigue leyendo la env a mano (dos fuentes = dos precedencias)"

    monkeypatch.setattr(auth_router.settings, "trusted_proxy_ips", "10.9.9.9")
    assert auth_router._trusted_proxies() == frozenset({"10.9.9.9"})
    monkeypatch.setattr(auth_router.settings, "trusted_proxy_ips", "")
    assert auth_router._trusted_proxies() == frozenset(), \
        "vacío tiene que significar 'no confiar en ningún XFF'"


def test_el_override_por_settings_cambia_a_quien_se_le_cree_el_xff(monkeypatch):
    """El campo no es decorativo: gobierna de verdad el bucket del rate-limit."""
    import apps.web.routers.auth as auth_router

    auth_router._login_attempts.clear()
    try:
        monkeypatch.setattr(auth_router.settings, "trusted_proxy_ips", "10.9.9.9")
        with TestClient(app, client=("127.0.0.1", 4242)) as c:   # ya NO es confiable
            c.post("/login", data={"username": "admin", "password": "mala"},
                   headers={"X-Forwarded-For": "203.0.113.7"})
        assert list(auth_router._login_attempts) == [("127.0.0.1", "admin")], \
            auth_router._login_attempts
    finally:
        auth_router._login_attempts.clear()
