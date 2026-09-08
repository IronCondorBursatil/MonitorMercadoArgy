import logging
import time
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from apps.web.deps_auth import get_db
from apps.web.users_service import normalizar_email
from core.infrastructure.db.models import UserORM
from core.security import verify_password, create_access_token, get_password_hash, password_invalida
from apps.web.templates import TEMPLATES as _TEMPLATES
from apps.web import reset_service
from config.settings import settings

router = APIRouter()

# Rate-limiting en memoria de /login (por-proceso, 1 worker): frena fuerza bruta sin
# dependencia externa. Clave (ip, usuario): un atacante martillando 'admin' se bloquea
# para ese usuario/IP; el resto sigue entrando. Se limpia perezosamente por ventana.
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW_SEC = 300
_login_attempts: dict = defaultdict(list)
# Techo duro de claves vivas: el barrido por ventana ya las vence, esto acota el
# pico de un ataque distribuido (una clave = una tupla chica, ~4k es despreciable).
_MAX_TRACKED_KEYS = 4096
# Hash bcrypt dummy: verificar SIEMPRE (aunque el usuario no exista) iguala el tiempo
# de respuesta y evita enumerar usuarios por timing. Lazy para no pagar bcrypt al import.
_DUMMY_HASH = None

# Rate-limit de POST /reset/{token}: por IP, 10 / 15 min. El token tiene 256 bits (no se
# adivina), esto sólo frena martillar la ruta.
_MAX_RESET_ATTEMPTS = 10
_RESET_WINDOW_SEC = 900
_reset_attempts: dict = defaultdict(list)


def _rate_limited(bucket: dict, key, max_attempts: int, window: float) -> bool:
    """True si `key` ya agotó `max_attempts` en `window` segundos; registra el intento."""
    now = time.time()
    recent = [t for t in bucket[key] if now - t < window]
    if len(recent) >= max_attempts:
        bucket[key] = recent
        return True
    recent.append(now)
    bucket[key] = recent
    if len(bucket) > _MAX_TRACKED_KEYS:
        for k in sorted(bucket, key=lambda k: bucket[k][-1])[:len(bucket) - _MAX_TRACKED_KEYS]:
            bucket.pop(k, None)
    return False


def _trusted_proxies() -> frozenset:
    """Peers TCP cuyo X-Forwarded-For se cree (frontera de confianza del proxy).

    Sale de `settings.trusted_proxy_ips` y de NINGÚN otro lado: la convención del repo
    es que pydantic-settings sea el único lector del env (MONITOR_TRUSTED_PROXY_IPS).
    Leerlo acá con `os.environ` además invertía la precedencia — el `getattr(settings,
    ...)` que había ganaba en silencio el día que el campo existiera. Vacío = no
    confiar en ningún XFF (todo se imputa al peer TCP)."""
    return frozenset(p.strip() for p in str(settings.trusted_proxy_ips or "").split(",")
                     if p.strip())


def _client_ip(request: Request) -> str:
    """IP a la que se le imputan los intentos de login.

    El header X-Forwarded-For lo escribe el CLIENTE: si se lee sin más, el atacante
    elige un bucket nuevo por intento y el limiter no dispara nunca. Sólo se lo cree
    si el peer TCP es un proxy confiable, y de ahí se toma la ÚLTIMA entrada: nginx
    usa `$proxy_add_x_forwarded_for` = "$http_x_forwarded_for, $remote_addr", así que
    la que agregó NUESTRO proxy va al final; todo lo anterior es falseable.
    """
    peer = request.client.host if request.client else "?"
    if peer not in _trusted_proxies():
        return peer
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return peer
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    return parts[-1] if parts else peer


def _prune_login_attempts(now: float) -> None:
    """Barrido GLOBAL del contador (antes sólo se podaba la clave visitada: con una
    clave distinta por intento el dict quedaba creciendo sin vencimiento)."""
    stale = [k for k, ts in _login_attempts.items()
             if not ts or now - ts[-1] >= _LOGIN_WINDOW_SEC]
    for key in stale:
        _login_attempts.pop(key, None)
    if len(_login_attempts) > _MAX_TRACKED_KEYS:
        exceso = len(_login_attempts) - _MAX_TRACKED_KEYS
        for key, _ in sorted(_login_attempts.items(), key=lambda kv: kv[1][-1])[:exceso]:
            _login_attempts.pop(key, None)


# Destino post-login por pestaña, en el MISMO orden que el nav de base.html. La home
# `/` la sirve el router de paneles, montado con RequireTabPermission("bonos"): mandar
# a `/` a un usuario sin esa pestaña lo rebotaba al login para siempre (login OK → 302
# `/` → RequiresLoginException → 302 /login), sin ningún mensaje.
_TAB_LANDING = (
    ("bonos", "/"),
    ("on", "/on"),
    ("curva", "/curva"),
    ("cartera", "/cartera"),
    ("bcra", "/bcra"),
    ("cashflows", "/cashflows"),
    ("fci", "/fci"),
    ("escenarios", "/escenarios"),
    ("opciones", "/options"),
    ("catalogo", "/catalogo"),
    ("abm", "/abm"),
)


def _landing_url(user: UserORM):
    """Primera pestaña que el usuario SÍ puede ver (None si no tiene ninguna)."""
    tabs = user.allowed_tabs or []
    if user.is_admin or "*" in tabs:
        return "/"
    for tab, url in _TAB_LANDING:
        if tab in tabs:
            return url
    return None


# Auditoria: al journal (stdout) via el filtro de consola de settings, que deja pasar
# INFO solo si viene con `console=True`. Sin esto, un login exitoso o fallido no queda
# registrado en NINGUN lado: el handler de archivo es WARNING+ y el filtro de consola
# descarta los access 2xx/3xx de uvicorn.
_audit = logging.getLogger("monitor.audit")


def _limpio(v) -> str:
    """Un campo del usuario, apto para una linea de log.

    Sin esto, un usuario con `
` en el nombre parte el registro en dos y puede
    fabricar una linea de auditoria falsa (log injection).

    Pero el salto de linea no alcanza: el formato es logfmt (`login=fail user=%s
    ip=%s`) y el ESPACIO y el `=` tambien son `isprintable()`. Un login fallido con
    usuario `x ip=1.2.3.4 login=ok` forjaba campos enteros dentro del registro —y el
    username del login se loguea CRUDO, sin pasar por la validacion del alta. Los dos
    delimitadores se reemplazan por `_`; el resto del valor se conserva legible.
    Hallazgo de la auditoria 2026-09-04."""
    limpio = "".join(ch for ch in str(v) if ch.isprintable())[:64]
    return limpio.replace("=", "_").replace(" ", "_") or "-"


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = get_password_hash("x")
    return _DUMMY_HASH


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, reset: str = ""):
    # `?reset=ok` lo pone POST /reset/{token} al terminar: banner "ingresá con la nueva".
    return _TEMPLATES.TemplateResponse(request, "pages/login.html",
                                       {"error": None, "reset_ok": reset == "ok"})

@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    key = (_client_ip(request), (username or "").strip().lower())
    now = time.time()
    _prune_login_attempts(now)
    recent = [t for t in _login_attempts[key] if now - t < _LOGIN_WINDOW_SEC]
    _login_attempts[key] = recent
    if len(recent) >= _MAX_LOGIN_ATTEMPTS:
        _audit.info("auth login=ratelimited user=%s ip=%s", _limpio(username),
                    _limpio(key[0]), extra={"console": True})
        return _TEMPLATES.TemplateResponse(
            request, "pages/login.html",
            {"error": "Demasiados intentos. Esperá unos minutos."}, status_code=429)

    # El campo "Usuario" acepta usuario O email (spec §5.3). Primero el match exacto de
    # username (comportamiento histórico); si no hay, el email normalizado. El rate-limit
    # sigue clavado al valor tipeado y `verify_password` corre igual en los tres casos
    # (usuario, email, nada): sin oráculo de existencia.
    user = db.query(UserORM).filter(UserORM.username == username).first()
    if user is None:
        mail = normalizar_email(username)
        if mail and "@" in mail:
            user = db.query(UserORM).filter(UserORM.email == mail).first()
    # Verificar SIEMPRE un hash (contra el real o el dummy) → mismo tiempo con/sin usuario.
    ok = verify_password(password, user.hashed_password) if user else verify_password(password, _dummy_hash())
    # Un usuario DESHABILITADO recibe exactamente la misma respuesta que una clave
    # incorrecta: no se le confirma que la cuenta existe. El motivo va sólo al log.
    if not user or not ok or not user.is_active:
        _login_attempts[key].append(now)
        _audit.info("auth login=fail user=%s ip=%s%s", _limpio(username), _limpio(key[0]),
                    " motivo=deshabilitado" if (user and ok) else "", extra={"console": True})
        return _TEMPLATES.TemplateResponse(request, "pages/login.html", {"error": "Usuario o contraseña incorrectos"})

    _login_attempts.pop(key, None)   # login OK → limpiar el contador
    user.last_login_at = datetime.now()
    user.last_login_ip = key[0][:64]
    db.commit()
    _audit.info("auth login=ok user=%s ip=%s", _limpio(user.username), _limpio(key[0]),
                extra={"console": True})

    landing = _landing_url(user)
    if landing is None:
        # Sin ninguna pestaña habilitada no hay a dónde mandarlo: decirlo, en vez de
        # dejarlo rebotando entre `/` y `/login` como si la clave estuviera mal.
        return _TEMPLATES.TemplateResponse(
            request, "pages/login.html",
            {"error": "Tu usuario no tiene ningún módulo habilitado. "
                      "Pedile acceso a un administrador."}, status_code=403)

    # Generar token JWT
    access_token = create_access_token(
        data={"sub": user.username, "ver": user.token_version or 0})

    # Redirigir a la primera pestaña permitida seteando la cookie
    response = RedirectResponse(url=landing, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,   # True con TLS → la cookie no viaja por HTTP
        max_age=settings.jwt_access_token_expire_minutes * 60,
    )
    return response

@router.post("/logout")
def logout():
    # POST (no GET) para que no sea CSRF-eable: con la cookie SameSite=Lax, un GET
    # state-changing embebido cross-site igual se dispararía (logout forzado).
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("access_token")
    return response


# ── Nueva contraseña por link (reseteo o invitación) ──────────────────────────
# Pública a sabiendas (tests/test_aud_G_tests_route_auth.py::_PUBLIC_PATHS): la
# credencial es el token. Toda invalidez (inexistente, vencido, usado, usuario
# deshabilitado) responde la MISMA página con 200: sin oráculo.
_INVALIDO = "pages/reset_invalido.html"


def _form_reset(request, user, token, error=None, status_code=200):
    return _TEMPLATES.TemplateResponse(
        request, "pages/reset.html",
        {"nombre": (user.full_name or user.username).split()[0], "username": user.username,
         "token": token, "error": error}, status_code=status_code)


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_page(request: Request, token: str, db: Session = Depends(get_db)):
    found = reset_service.lookup_reset_token(db, token)
    if found is None:
        return _TEMPLATES.TemplateResponse(request, _INVALIDO, {})
    user, _row = found
    return _form_reset(request, user, token)


@router.post("/reset/{token}", response_class=HTMLResponse)
def reset_submit(request: Request, token: str, password: str = Form(""),
                 password2: str = Form(""), db: Session = Depends(get_db)):
    ip = _client_ip(request)
    if _rate_limited(_reset_attempts, ip, _MAX_RESET_ATTEMPTS, _RESET_WINDOW_SEC):
        _audit.info("auth reset=ratelimited ip=%s", _limpio(ip), extra={"console": True})
        return _TEMPLATES.TemplateResponse(request, _INVALIDO, {"ratelimited": True}, status_code=429)
    found = reset_service.lookup_reset_token(db, token)
    if found is None:
        return _TEMPLATES.TemplateResponse(request, _INVALIDO, {})
    user, row = found
    if password != password2:
        return _form_reset(request, user, token, error="Las dos contraseñas no coinciden.", status_code=400)
    invalida = password_invalida(password)
    if invalida:
        return _form_reset(request, user, token, error=invalida, status_code=400)
    reset_service.consume_reset_token(db, token, password)
    _audit.info("auth reset_consumed purpose=%s target=%s ip=%s", _limpio(row.purpose),
                _limpio(user.username), _limpio(ip), extra={"console": True})
    return RedirectResponse(url="/login?reset=ok", status_code=status.HTTP_303_SEE_OTHER)
