---
paths:
  - "apps/web/deps_auth.py"
  - "apps/web/routers/auth.py"
  - "apps/web/routers/users_abm.py"
  - "core/security.py"
  - "apps/web/users_service.py"
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
  `/logout`, `/api/health`, `/reset/{token}`, `/forgot`) + `/static`. Agregar una es cambiar ese
  test a sabiendas.

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

## Usuarios (Manager v2, spec 2026-09-08)

- Rutas del admin (`routers/users_abm.py`, todas bajo `get_admin_user_html`): `GET /users[?u=id]`,
  `GET /users/{id}/ficha` (fragmento HTMX), `POST /users/add`, `POST /users/{id}/datos`,
  `/permisos`, `/reset` (form channel: manual | link | mail), `/sesiones/cerrar`,
  `/estado` (form `activo` 0/1), `POST /users/delete/{id}`. Las viejas `/users/update/{id}` y
  `/users/reset-password/{id}` ya no existen.
- Tokens de reseteo/invitación: `apps/web/reset_service.py` (contrato: `secrets.token_urlsafe(32)`,
  se persiste SÓLO el SHA-256, un solo uso, vence a los 60 min (invitación 72 h), emitir uno
  nuevo invalida los vivos del usuario, usuario deshabilitado → inválido, ligado a la
  `token_version` del usuario al emitirlo: cualquier gesto que la suba (clave a mano, cerrar
  sesiones, deshabilitar) lo invalida, consumirlo sube `token_version`). Nunca loguear el token.
- `POST /users/add` acepta `access=invite|password` (default `password`); el invitado queda
  con `hashed_password="!"` (`core.security.SIN_PASSWORD_HASH`) hasta aceptar el link;
  `verify_password` devuelve False sin excepción para cualquier hash que no sea bcrypt,
  verificando igual contra el dummy.
- `/reset/{token}` (público a sabiendas): la MISMA página 200 para inexistente/vencido/usado/
  deshabilitado; éxito → 303 `/login?reset=ok`; rate-limit 10 por IP / 15 min.
- Canal `mail` de `/reset`: manda el correo DENTRO del request (`core/infrastructure/mailer.py`);
  si falla, el admin ve el error de envío Y el link copiable como respaldo — nunca se pierde la
  vía manual. `POST /users/add` con SMTP activo y el usuario con email ofrece "Enviar link por
  mail" (mismo respaldo si falla); sin SMTP o sin email el botón queda deshabilitado con el motivo.
- `GET/POST /forgot` (público, en `_PUBLIC_PATHS`): respuesta neutra siempre (nunca confirma si
  el usuario o el email existen), el envío corre por `BackgroundTasks` (no bloquea la respuesta);
  rate-limit propio, aparte del de `/login` y `/reset/{token}` — 3 intentos por IP / 15 min y 3
  por dato tipeado / 60 min (`_forgot_attempts_ip` / `_forgot_attempts_dato`, mismo `_rate_limited`
  del login). Un invitado (`hashed_password="!"`) queda excluido: no se le puede pedir reset ahí.
- `mailer.py` (`send_mail`): `smtplib` + STARTTLS, sin dependencia nueva; timeout 15 s
  (`SMTP_TIMEOUT_S`); sin `MONITOR_SMTP_HOST` levanta `MailNotConfigured`
  (`settings.mail_enabled = bool(smtp_host)`, gatea canal mail/invitación/`/forgot`). Variables
  `MONITOR_SMTP_*` y la contraseña de aplicación de Gmail: `docs/despliegue.md`.
- El login acepta usuario o email (`normalizar_email`); la clave del rate-limit sigue siendo
  el valor tipeado.
- **Toda respuesta HTML de la ABM pasa por `_users_page`** (arma filas, resumen y ficha
  seleccionada); no llamar `TemplateResponse("pages/users.html")` a mano.
- Reglas puras (email, estado, actividad derivada, `TABS`) en `apps/web/users_service.py`, sin
  FastAPI: se testean solas.
- `is_active=0` (deshabilitado): el login responde EXACTAMENTE igual que una clave incorrecta
  (no confirma que la cuenta existe) y `deps_auth._get_user_from_token` rechaza la cookie aunque
  la `token_version` coincida. No se puede deshabilitar a uno mismo ni al último admin activo.
- Resetear la contraseña (canal manual) o "Cerrar sesiones" suben `token_version` y cierran las
  sesiones de ese usuario (commit `5452c3f`); cambiar permisos NO (se releen por request).
- Política de contraseña única: `core/security.password_invalida` (10 chars / 72 bytes).
- No hay bootstrap automático del admin: la única receta es `scripts/init_admin.py` con
  `MONITOR_ADMIN_PASSWORD` (`docs/despliegue.md › Primer arranque`). Nunca un default
  hardcodeado.

## Tests

- La fixture autouse `_auth_bypass` (`tests/conftest.py`) corre todo como admin: overridea
  `get_current_user*` y parchea `templates._get_user_from_token`. Un test de auth REAL lleva
  `@pytest.mark.noauth` (`tests/test_auth.py`).
- El guard "toda ruta exige login" recorre las rutas con `tests/_routes.py`; nunca
  `app.routes` a secas (FastAPI ≥ 0.141 lo dejaba ciego).
