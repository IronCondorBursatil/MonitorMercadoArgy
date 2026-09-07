# Autenticación y permisos

Movido desde `CLAUDE.md` en la Fase 4 (agents.md §0.8). Al tocar `deps_auth.py`,
`routers/auth.py`, `routers/users_abm.py` o `core/security.py` carga sola
`.claude/rules/auth.md`. Correr `/security-review` antes de pushear cualquier cambio acá
o en routers (agents.md §0.1.13).

## Sesión

Toda la app está detrás de login (`apps/web/deps_auth.py` + `routers/auth.py` +
`core/security.py`). JWT en cookie httponly (`access_token`), firmado HS256; contraseñas
con bcrypt (`core/security.py`). **El secreto NO es hardcodeado**: se resuelve en
`settings.model_post_init` → env `MONITOR_JWT_SECRET_KEY` > archivo `db_dir/jwt_secret`
(0600, fuera del working tree) > generado y persistido al vuelo. En prod, setear
`MONITOR_JWT_SECRET_KEY` (ver `docs/despliegue.md`; la rotación es decisión de David,
`docs/decisiones.md` D3).

## Permisos por pestaña

`UserORM.allowed_tabs` (JSON) + `RequireTabPermission("<tab>")` como `dependencies=` de
cada router en `app.py`. `is_admin` bypasea; `"*"` = todas. Los routers de lectura global
(`header`, `source`, `stream`) van con `get_current_user_html` (solo login); `/source/*`
POST (conmutar la fuente, guardar credenciales BYMA) y `/users/*` exigen **admin**.
`/api/health` es público pero recortado (sin `last_error`, sin tickers). El conjunto de
rutas públicas lo fija `tests/test_aud_G_tests_route_auth.py::_PUBLIC_PATHS`.

Falta de **permiso** ≠ falta de **login**: `RequireTabPermission` levanta
`TabForbiddenException` → **403** con la lista de pestañas habilitadas (`deps_auth.py` +
handler en `app.py`). Sólo el no-autenticado (`RequiresLoginException`) va a 302 `/login`.

## Rate-limit del login

`routers/auth.py`: 5 intentos / 5 min (`_LOGIN_WINDOW_SEC = 300`) por (IP, usuario),
429 al excederlo. La IP sale del peer TCP y sólo se cree el `X-Forwarded-For` si ese peer
está en `settings.trusted_proxy_ips` (default `127.0.0.1,::1`; override
`MONITOR_TRUSTED_PROXY_IPS`, lista por comas, vacío = no confiar en ningún XFF). Supone
**UN** solo proxy y que la ÚLTIMA entrada del XFF la escribió él (nginx
`$proxy_add_x_forwarded_for`): con un CDN delante de nginx esa entrada pasa a ser la IP
del CDN y el limiter agrupa a todos en un bucket único — habría que setear ahí la IP del
CDN y tomar otra posición del header.

## CSRF y headers

`apps/web/security_web.py`: validación de origen para POST/PUT/PATCH/DELETE
(`reject_cross_site`) + `SecurityHeadersMiddleware`. Detalle en `docs/flujo-web.md ›
Defensas de borde`.

## Cookie `secure`

`settings.cookie_secure = False` **a propósito** mientras producción sirva por HTTP (sin
dominio no hay certificado). Activarlo sin HTTPS hace que el browser descarte la cookie
→ login en loop. El pasaje a HTTPS y el momento de `MONITOR_COOKIE_SECURE=true` están en
`docs/despliegue.md`.

## Al testear la web

`tests/conftest.py` tiene una fixture autouse `_auth_bypass` que corre los tests como
admin (overridea `get_current_user*` + parchea `templates._get_user_from_token`). Un test
que ejerza la auth REAL marca `@pytest.mark.noauth` (ver `tests/test_auth.py`).

## Primer admin

No hay bootstrap automático de usuarios: ni el lifespan ni `deploy.sh` crean el admin. La
receta (una sola, para local y prod) está en `docs/despliegue.md › Primer arranque`.
