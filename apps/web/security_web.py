"""Defensas de borde de la app web: validación de origen (CSRF) y headers.

Las dos existen porque el Monitor pasó a servirse desde una IP pública. No pretenden
convertirlo en un banco: son las dos cosas que cuestan poco y cubren las clases de
ataque que un servidor expuesto recibe sin que nadie lo apunte (bots que prueban
formularios, páginas hostiles que hacen submit cruzado, clickjacking).
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders

from config.settings import settings

logger = logging.getLogger(__name__)

# Métodos que cambian estado. GET/HEAD/OPTIONS no se validan: la app no tiene rutas
# de mutación por GET (verificado) y validarlas rompería la navegación normal.
_METODOS_INSEGUROS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _host_de(url: str) -> str:
    """netloc de una URL, SIN el esquema.

    Ignorar el esquema es deliberado: cuando el sitio pase a HTTPS, el `Origin` va a
    decir `https://…` y el `Host` sigue siendo el mismo. Comparar la URL completa
    obligaría a tocar esto en el mismo deploy que activa TLS.
    """
    try:
        return (urlsplit(url).netloc or "").lower()
    except ValueError:
        return ""


def _hosts_confiables(request: Request) -> set:
    hosts = {(request.headers.get("host") or "").lower()}
    hosts |= {h.strip().lower() for h in (settings.csrf_trusted_hosts or "").split(",")
              if h.strip()}
    return {h for h in hosts if h}


async def reject_cross_site(request: Request) -> None:
    """Rechaza con 403 las mutaciones que vienen de otro sitio.

    Se registra como dependencia a nivel de app, así que corre ANTES que las de auth:
    un POST cruzado se corta sin abrir una sesión de base ni mirar la cookie.

    Orden de decisión (el mismo de `CrossOriginProtection` de Go 1.25):
      1. `Sec-Fetch-Site` — el dato más confiable, lo pone el browser y no es
         falsificable desde JS. `same-origin`/`none` pasan; el resto se rechaza.
      2. `Origin` contra el `Host` del request.
      3. `Referer`, mismo criterio.
      4. Si NO viene ninguno de los tres: se permite.

    El punto 4 es el que hace que esto sea aplicable sin romper nada, y merece la
    explicación: CSRF necesita un browser, y todo browser capaz de montarlo manda
    `Origin` en un POST (desde 2019) y `Sec-Fetch-Site` (Chrome 76+, Firefox 90+,
    Safari 16.4+). Lo que NO manda ninguno de los tres es `curl`, un script, o el
    `TestClient` de Starlette — y ninguno de ellos es un vector de CSRF, porque el
    atacante ya controlaría la máquina. Bloquearlos sólo rompería los 15 archivos de
    tests que hacen `c.post(...)` sin comprar seguridad.

    `/login` NO se exime: el login-CSRF (forzarte a iniciar sesión como el atacante)
    es real, y el formulario propio es same-origin, así que pasa.

    DEPENDE de que nginx mande `proxy_set_header Host $host`. El default de nginx es
    `$proxy_host` (127.0.0.1:8000), con el que TODO POST de browser daría 403. Está
    en `deploy/nginx/monitores.conf` y hay un test que lo fija. La válvula de escape
    es `MONITOR_CSRF_TRUSTED_HOSTS`.
    """
    if request.method not in _METODOS_INSEGUROS:
        return

    sitio = (request.headers.get("sec-fetch-site") or "").lower()
    if sitio:
        if sitio in ("same-origin", "none"):
            return
        _rechazar(request, f"Sec-Fetch-Site: {sitio}")

    confiables = _hosts_confiables(request)
    for cabecera in ("origin", "referer"):
        valor = request.headers.get(cabecera)
        if not valor:
            continue
        if _host_de(valor) in confiables:
            return
        _rechazar(request, f"{cabecera}: {valor}")

    # Sin metadatos de fetch: no es un browser (ver docstring).
    return


def _rechazar(request: Request, motivo: str) -> None:
    logger.warning("CSRF: %s %s rechazado (%s; host=%s)",
                   request.method, request.url.path, motivo,
                   request.headers.get("host"))
    raise HTTPException(status_code=403, detail="Origen cruzado rechazado")


class SecurityHeadersMiddleware:
    """Headers de seguridad, en ASGI puro.

    NO usa `BaseHTTPMiddleware` a propósito: envuelve el `receive` del scope, y
    `routers/stream.py` ya documenta que el listener de desconexión de sse-starlette
    depende de ese mismo `receive`. Un middleware que lo envuelva rompe el SSE.
    Acá sólo se toca `http.response.start`, que no interviene en el cuerpo.

    Sobre la CSP: va ENFORCED desde el día uno, pero con `'unsafe-inline'` y
    `'unsafe-eval'`. No es pereza —
      * `'unsafe-eval'` es OBLIGATORIO: htmx 2.0.3 compila los filtros de trigger
        (`[mrRefreshOK(this)]`, 15 usos) con `Function`;
      * `'unsafe-inline'` lo exigen los 20 bloques `<script>` y los 107 handlers
        `on*=` repartidos en 26 templates. Los nonces no cubren atributos.
    Lo que igual compra: `object-src 'none'`, `base-uri`, `form-action` y
    `frame-ancestors` (clickjacking), y `connect-src 'self'` — que impide exfiltrar
    datos a un tercero aunque se colara un script. Pasar a un `script-src` estricto
    exige refactorizar los 107 handlers a listeners delegados: registrado y no hecho.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_con_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("x-content-type-options", "nosniff")
                headers.setdefault("x-frame-options", "DENY")
                # `same-origin` y no `strict-origin-when-cross-origin`: no hay ni un
                # link saliente en los templates, así que no se pierde nada, y el
                # Referer same-origin sigue intacto para el fallback de arriba.
                headers.setdefault("referrer-policy", "same-origin")
                headers.setdefault(
                    "permissions-policy",
                    "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
                if settings.csp_policy:
                    headers.setdefault("content-security-policy", settings.csp_policy)
            await send(message)

        await self.app(scope, receive, send_con_headers)
