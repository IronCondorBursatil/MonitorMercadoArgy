---
paths:
  - "apps/web/deps_auth.py"
  - "apps/web/routers/auth.py"
  - "apps/web/routers/users_abm.py"
  - "core/security.py"
---

# Auth — reglas que cargan al tocar login, permisos o usuarios

Detalle en `docs/auth.md`. Correr **`/security-review` antes de pushear** cualquier cambio
acá o en routers (agents.md §0.1.13). No debilitar validaciones para que un test pase.

## Sesión y secreto

- JWT HS256 en cookie **httponly** `access_token`; bcrypt para contraseñas (`core/security.py`:
  `verify_password`, `get_password_hash`, `create_access_token`, `decode_access_token`).
- El secreto NO se hardcodea: `settings.model_post_init` → env `MONITOR_JWT_SECRET_KEY` >
  archivo `db_dir/jwt_secret` (0600, fuera del árbol) > generado y persistido. Rotarlo
  invalida todas las sesiones (esperado) y es decisión de David (`docs/decisiones.md` D3);
  el agente no rota secretos sin OK explícito (agents.md §0.1.6). `jwt_secret` está en
  `deny` de lectura para el agente.

## 403 vs 302

- `RequireTabPermission("<tab>")` como `dependencies=` de cada router en `app.py`;
  `UserORM.allowed_tabs` (JSON), `"*"` = todas, `is_admin` bypasea.
- Falta de **permiso** → `TabForbiddenException` → **403** con la lista de pestañas
  habilitadas. Falta de **login** → `RequiresLoginException` → **302** `/login`. No mezclar:
  un 302 por falta de permiso esconde el problema; un 403 al anónimo filtra que la ruta existe.
- `get_current_user_html` = sólo login (routers `header`, `source`, `stream`). Admin
  obligatorio en `/source/*` POST y `/users/*`.
- Rutas públicas = `tests/test_aud_G_tests_route_auth.py::_PUBLIC_PATHS` (`/login`,
  `/logout`, `/api/health`) + `/static`. Agregar una es cambiar ese test a sabiendas.

## Rate-limit del login

- 5 intentos / 5 min (`_LOGIN_WINDOW_SEC = 300`) por (IP, usuario) → 429.
- La IP es la del peer TCP; `X-Forwarded-For` sólo se cree si el peer está en
  `settings.trusted_proxy_ips` (default `127.0.0.1,::1`; `MONITOR_TRUSTED_PROXY_IPS`, vacío =
  no confiar en ninguno). Supone UN proxy y que la ÚLTIMA entrada del XFF es suya (nginx
  `$proxy_add_x_forwarded_for`). Con un CDN delante habría que tomar otra posición del header.

## Cookie `secure`

`settings.cookie_secure = False` **a propósito**: prod sirve por HTTP (sin dominio no hay
certificado) y con `secure` el browser descarta la cookie → login en loop. Se activa
(`MONITOR_COOKIE_SECURE=true`) recién después de `deploy/setup-https.sh` (`docs/despliegue.md`).
uvicorn ≥ 0.48 ya honra `X-Forwarded-Proto` por default; no tocar el `ExecStart`.

## Usuarios

- Resetear una contraseña cierra las sesiones de ese usuario (commit `5452c3f`); mantenerlo.
- No hay bootstrap automático del admin: la única receta es `scripts/init_admin.py` con
  `MONITOR_ADMIN_PASSWORD` (`docs/despliegue.md › Primer arranque`). Nunca un default
  hardcodeado.

## Tests

- La fixture autouse `_auth_bypass` (`tests/conftest.py`) corre todo como admin: overridea
  `get_current_user*` y parchea `templates._get_user_from_token`. Un test de auth REAL lleva
  `@pytest.mark.noauth` (`tests/test_auth.py`).
- El guard "toda ruta exige login" recorre las rutas con `tests/_routes.py`; nunca
  `app.routes` a secas (FastAPI ≥ 0.141 lo dejaba ciego).
