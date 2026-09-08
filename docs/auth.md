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
rutas públicas lo fija `tests/test_aud_G_tests_route_auth.py::_PUBLIC_PATHS` (incluye
`/reset/{token}`, público a sabiendas y con su propio rate-limit, ver más abajo).

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

## Manager de usuarios

Rediseñado el 2026-09-08 (spec `docs/superpowers/specs/2026-09-08-manager-usuarios-reseteo-design.md`,
Opción A: tabla + ficha lateral HTMX). `users` tiene email (único si no es NULL, índice parcial
`ux_users_email`), nombre, notas, `is_active`, alta (cuándo/quién), último ingreso (fecha/IP) y
fecha del último cambio de contraseña; todo entró por la migración forward-only de `init_db`.
Las reglas puras viven en `apps/web/users_service.py`; el router `routers/users_abm.py` tiene una
ruta POST por acción y responde siempre con `_users_page`. Deshabilitar una cuenta bloquea el
login (misma respuesta que una clave incorrecta) y mata la sesión viva en el siguiente request.
Las páginas sin sesión (`/login`, `/reset/{token}`; en la fase siguiente también `/forgot`)
extienden `templates/base_public.html`: header de la app sin nav. El login (`POST /login`)
acepta usuario o email indistintamente (busca `username == valor` o
`email == normalizar_email(valor)`); la clave del rate-limit sigue siendo el valor tal cual
se tipeó. La Fase 3 (mail, autoservicio `/forgot`) está descrita en la spec.

**Reseteo por link e invitación** (Fase 2). Los tokens viven en `password_reset_tokens` y los
maneja `apps/web/reset_service.py`: `secrets.token_urlsafe(32)` de 256 bits, se persiste SÓLO
el SHA-256 (`core.security.hash_token`), nunca el token en claro ni en un log; un solo uso;
vencen a los 60 minutos (`reset`) o 72 horas (`invite`); emitir uno nuevo invalida los vivos
del mismo usuario; un usuario deshabilitado no puede consumirlo; consumirlo sube
`token_version` (cierra las demás sesiones) y actualiza `password_changed_at`. El admin lo
dispara desde el Manager por el canal `link` (`POST /users/{id}/reset channel=link`): el link
se arma con `settings.public_url` (`MONITOR_PUBLIC_URL`, si no está seteado cae al
`request.base_url`) y se muestra UNA sola vez en la ficha, para copiar. El alta por invitación
(`POST /users/add access=invite`) crea el usuario con `hashed_password="!"`
(`core.security.SIN_PASSWORD_HASH`, un centinela que no es un hash bcrypt) y un token `invite`
de 72 h; no exige email, el link es lo que se le pasa al invitado. `GET/POST /reset/{token}`
es público a sabiendas (`_PUBLIC_PATHS`): la MISMA página 200 "Este link ya no sirve" cubre
token inexistente, vencido, ya usado o de un usuario deshabilitado, sin distinguir el motivo;
al elegir contraseña con éxito redirige 303 a `/login?reset=ok` (banner). `POST /reset/{token}`
tiene su propio rate-limit, 10 intentos por IP cada 15 minutos, aparte del de `/login`.

## Al testear la web

`tests/conftest.py` tiene una fixture autouse `_auth_bypass` que corre los tests como
admin (overridea `get_current_user*` + parchea `templates._get_user_from_token`). Un test
que ejerza la auth REAL marca `@pytest.mark.noauth` (ver `tests/test_auth.py`).

## Primer admin

No hay bootstrap automático de usuarios: ni el lifespan ni `deploy.sh` crean el admin. La
receta (una sola, para local y prod) está en `docs/despliegue.md › Primer arranque`.
