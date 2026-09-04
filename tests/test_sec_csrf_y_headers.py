"""Validación de origen (CSRF) y headers de seguridad.

Contexto: el Monitor pasó a servirse desde una IP pública, en HTTP plano. No había
ninguna defensa contra submit cruzado más allá de `SameSite=Lax`, ni un solo header
de seguridad (el único middleware era GZip).
"""

import pytest
from fastapi.testclient import TestClient

from apps.web.app import app

pytestmark = pytest.mark.noauth   # auth REAL: el bypass de conftest no aplica


# ── CSRF ───────────────────────────────────────────────────────────────────
def test_un_post_desde_otro_sitio_se_rechaza():
    """El caso que la defensa existe para cubrir: una página hostil que hace submit
    a nuestro endpoint con la cookie del usuario."""
    with TestClient(app) as c:
        r = c.post("/logout", headers={"Origin": "http://evil.example"},
                   follow_redirects=False)
    assert r.status_code == 403


def test_sec_fetch_site_cross_site_se_rechaza_aunque_no_venga_Origin():
    """`Sec-Fetch-Site` lo pone el browser y no es falsificable desde JS: es la señal
    más confiable y se mira PRIMERO."""
    with TestClient(app) as c:
        r = c.post("/logout", headers={"Sec-Fetch-Site": "cross-site"},
                   follow_redirects=False)
    assert r.status_code == 403


def test_el_mismo_sitio_pasa():
    with TestClient(app) as c:
        r = c.post("/logout", headers={"Origin": "http://testserver"},
                   follow_redirects=False)
        assert r.status_code in (302, 303), r.status_code
        r2 = c.post("/logout", headers={"Sec-Fetch-Site": "same-origin"},
                    follow_redirects=False)
        assert r2.status_code in (302, 303)


def test_sin_metadatos_de_fetch_se_permite():
    """La decisión que hace esto aplicable: `curl`, un script o el propio TestClient
    no mandan Origin/Referer/Sec-Fetch-Site, y NINGUNO es un vector de CSRF (para
    usarlos el atacante ya tendría la máquina). Bloquearlos rompería los 15 archivos
    de tests que hacen `c.post(...)` sin comprar nada."""
    with TestClient(app) as c:
        r = c.post("/logout", follow_redirects=False)
    assert r.status_code in (302, 303)


def test_el_esquema_no_importa():
    """Cuando el sitio pase a HTTPS el `Origin` va a decir `https://` y el `Host`
    sigue igual. Comparar la URL entera obligaría a tocar esto en el mismo deploy que
    activa TLS."""
    with TestClient(app) as c:
        r = c.post("/logout", headers={"Origin": "https://testserver"},
                   follow_redirects=False)
    assert r.status_code in (302, 303)


def test_el_GET_no_se_valida():
    """No hay rutas de mutación por GET; validarlas rompería la navegación normal
    (un link entrante trae Referer de otro sitio)."""
    with TestClient(app) as c:
        r = c.get("/login", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200


def test_login_tampoco_se_exime():
    """Login-CSRF (forzarte a iniciar sesión como el atacante) es real. El formulario
    propio es same-origin, así que pasa igual."""
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "x", "password": "y"},
                   headers={"Origin": "http://evil.example"}, follow_redirects=False)
    assert r.status_code == 403


def test_corre_antes_que_la_auth():
    """La dependencia es de nivel app: un POST cruzado se corta ANTES de abrir una
    sesión de base o resolver el usuario. Si corriera después, un POST cruzado sin
    cookie daría 302 a /login en vez de 403."""
    with TestClient(app) as c:
        r = c.post("/users/delete/1", headers={"Origin": "http://evil.example"},
                   follow_redirects=False)
    assert r.status_code == 403, (
        f"dio {r.status_code}: la validación de origen no corrió antes que la auth")


def test_hay_valvula_de_escape_por_env(monkeypatch):
    """Si un proxy no reenvía `Host`, TODO POST de browser daría 403. La perilla
    tiene que existir y no exigir un deploy."""
    from config.settings import settings

    monkeypatch.setattr(settings, "csrf_trusted_hosts", "evil.example")
    with TestClient(app) as c:
        r = c.post("/logout", headers={"Origin": "http://evil.example"},
                   follow_redirects=False)
    assert r.status_code in (302, 303)


# ── headers ────────────────────────────────────────────────────────────────
_ESPERADOS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
}


@pytest.mark.parametrize("ruta", ["/login", "/api/health"])
def test_los_headers_van_en_toda_respuesta(ruta):
    with TestClient(app) as c:
        h = c.get(ruta).headers
    for k, v in _ESPERADOS.items():
        assert h.get(k) == v, f"{ruta}: falta {k}"
    assert "permissions-policy" in h
    assert "content-security-policy" in h


def test_los_headers_tambien_en_los_estaticos():
    """El mount de /static está fuera del árbol de routers; el middleware es global
    y tiene que alcanzarlo igual."""
    with TestClient(app) as c:
        h = c.get("/static/vendor/htmx.min.js").headers
    assert h.get("x-content-type-options") == "nosniff"


def test_la_csp_conserva_lo_que_htmx_necesita():
    """`'unsafe-eval'` no es pereza: htmx 2.0.3 compila los filtros de trigger
    (`[mrRefreshOK(this)]`) con `Function`. Sacarlo apaga en silencio el gateo del
    auto-refresh de TODOS los paneles."""
    with TestClient(app) as c:
        csp = c.get("/login").headers["content-security-policy"]
    assert "'unsafe-eval'" in csp
    # …y lo que la política SÍ compra, que es por lo que vale la pena tenerla:
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "connect-src 'self'" in csp, "sin esto un script inyectado podría exfiltrar"


def test_la_csp_se_puede_apagar_por_env(monkeypatch):
    """Perilla de emergencia: si la política rompe algo en producción, se apaga por
    variable de entorno sin esperar un deploy."""
    from config.settings import settings

    monkeypatch.setattr(settings, "csp_policy", "")
    with TestClient(app) as c:
        h = c.get("/login").headers
    assert "content-security-policy" not in h
    assert h.get("x-content-type-options") == "nosniff", "los otros headers siguen"


def test_el_middleware_no_rompe_el_SSE():
    """`BaseHTTPMiddleware` envuelve el `receive` del scope y `routers/stream.py`
    documenta que el listener de desconexión de sse-starlette depende de él. Este
    middleware es ASGI puro y sólo toca `http.response.start`."""
    from apps.web import security_web

    assert not hasattr(security_web.SecurityHeadersMiddleware, "dispatch"), (
        "parece un BaseHTTPMiddleware: eso rompe el SSE")


# ── exposición ─────────────────────────────────────────────────────────────
def test_la_app_escucha_en_loopback_por_default():
    """El único que habla con internet es nginx. Con 0.0.0.0 —el default anterior—
    cualquier despliegue sin firewall exponía uvicorn directo en el 8000."""
    from config.settings import Settings

    assert Settings.model_fields["host"].default == "127.0.0.1"
