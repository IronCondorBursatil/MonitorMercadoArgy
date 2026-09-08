# Manager de usuarios v2 + reseteo de contraseña por mail — diseño (delta)

**Fecha**: 2026-09-08 · **Estado**: diseño aprobado en brainstorming; spec a revisar por David antes del plan.
**Mockups**: https://claude.ai/code/artifact/7293a9a2-95bb-4028-bf13-a404ba575959 (Opción A elegida; flujo en la
página 2; B y C archivadas en la página 3).

Spec tipo delta (agents.md §0.1.13): describe sólo lo que cambia. El sistema actual está en `docs/auth.md`,
`.claude/rules/auth.md`, `apps/web/routers/users_abm.py` y `apps/web/routers/auth.py`.

## 0. Decisiones tomadas (2026-09-08, David)

| Decisión | Elegido |
|---|---|
| Layout del Manager | **Opción A**: tabla + ficha en panel lateral (fragmento HTMX) |
| Remitente de mail | **Gmail con contraseña de aplicación**, SMTP 587 STARTTLS |
| HTTPS | **No hay dominio ni lo va a haber por un largo rato**: el flujo se diseña para HTTP con mitigaciones (§7) |
| Autoservicio | **Sí**: "¿Olvidaste tu contraseña?" en el login, respuesta neutra y rate-limit |
| Alta de usuarios | **Invitación por mail por defecto**; contraseña inicial a mano como alternativa |
| Login | Mismo header, colores y layout que el resto del proyecto (hoy `login.html` usa variables CSS inexistentes) |

## 1. Alcance

**Entra**
1. Manager `/users` rediseñado (Opción A) con más información por usuario: nombre, email, notas internas,
   estado (activo / deshabilitado / invitación pendiente / sin email), último ingreso, fecha de alta y de
   último cambio de contraseña, actividad reciente derivada.
2. Acciones nuevas del admin: editar datos, deshabilitar/habilitar, cerrar sesiones, restablecer contraseña
   por tres canales (mail · link copiable · a mano), alta por invitación.
3. Página pública `/reset/{token}` donde el cliente elige su propia contraseña (sirve para reseteo y para
   aceptar una invitación) y página `/forgot` de autoservicio.
4. Envío de mails por SMTP (sin dependencia nueva) con plantillas de reseteo e invitación.
5. Login y páginas públicas con el look del resto de la app (header + tokens de `app.css`).

**No entra** (se anota, no se hace): "Mi cuenta" para que un usuario logueado cambie su clave con la actual;
`must_change_password`; 2FA; HTTPS (camino posible en §7, spike aparte); historial de auditoría persistido
más allá de lo derivable (§2.3).

## 2. Datos

Todo vive en `catalog.db` (ahí está `users`). Schema **forward-only**: columnas nuevas con `ALTER ADD COLUMN`
vía `_migrate_table_add_columns`; ninguna migración de datos (no sube `CURRENT_SCHEMA_VERSION`).

### 2.1 `users` — columnas nuevas (todas nullable salvo `is_active`)

| Columna | Tipo | Notas |
|---|---|---|
| `email` | TEXT | normalizado a minúsculas y sin espacios; **único cuando no es NULL** (`CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email ON users(email) WHERE email IS NOT NULL`, en el bloque de índices aditivos de `init_db`) |
| `full_name` | TEXT | nombre para mostrar |
| `notes` | TEXT | notas internas del admin |
| `is_active` | INTEGER NOT NULL DEFAULT 1 | 0 = deshabilitado: no puede loguearse, sus sesiones vivas mueren en el próximo request, no puede consumir tokens |
| `created_at`, `created_by` | TEXT, TEXT | filas previas quedan NULL y la UI muestra "—" |
| `last_login_at`, `last_login_ip` | TEXT, TEXT | los escribe `POST /login` en el éxito |
| `password_changed_at` | TEXT | lo escriben los tres canales de reseteo y el consumo de token |

Fechas: `datetime.now()` naive en hora del proceso (ART por `apply_timezone`), como el resto de la app.

### 2.2 `password_reset_tokens` — tabla nueva (la crea `create_all`)

| Columna | Notas |
|---|---|
| `id` | PK |
| `user_id` | FK a `users.id`, con índice |
| `token_hash` | SHA-256 hex del token, UNIQUE. **El token en claro nunca se persiste ni se loguea** |
| `purpose` | `reset` \| `invite` |
| `channel` | `mail` \| `link` \| `self` (autoservicio) |
| `created_at`, `created_by` | `created_by` = id del admin; NULL en autoservicio |
| `expires_at` | reset: +60 min · invite: +72 h |
| `used_at` | NULL mientras esté vivo |

Limpieza perezosa: al emitir un token se borran los que vencieron hace más de 30 días (sin loop nuevo).

### 2.3 Actividad reciente = derivada, sin tabla de eventos

La sección "Actividad" de la ficha se arma con lo que ya hay: `last_login_at` (ingreso), `password_changed_at`
(cambio de clave), filas de `password_reset_tokens` (link emitido por canal / consumido / invitación aceptada),
`created_at`+`created_by` (alta). No se agrega una tabla de eventos (YAGNI); el log de auditoría sigue siendo
`monitor.audit`.

## 3. Contrato de los tokens (`core/security.py`)

1. **Emitir** `issue_reset_token(db, user, purpose, channel, by) -> str`: `secrets.token_urlsafe(32)`; se
   guarda su SHA-256; se **invalidan todos los tokens vivos del mismo usuario** (`used_at = now`) para que
   sólo el último link sirva. Devuelve el token en claro UNA vez.
2. **Validar** `lookup_reset_token(db, token) -> UserORM | None`: hash → fila; válido sólo si `used_at IS NULL`,
   `expires_at > now`, `user.is_active` y `row.token_version == user.token_version` (el token queda ligado
   a la versión de sesión con la que se emitió; hallazgo del security review 2026-09-08). Cualquier otro
   caso devuelve `None` sin distinguir motivo.
3. **Consumir** `consume_reset_token(db, token, new_password)`: valida la contraseña con la política única
   (§3.1); setea `hashed_password`, `password_changed_at = now`, `token_version += 1` (cierra las demás
   sesiones), `used_at = now`; auditoría `reset_consumed purpose=… target=…`.
4. **Usuario invitado** = fila con `hashed_password = "!"` (centinela "sin contraseña"). `verify_password`
   trata cualquier hash que no sea bcrypt como inválido **verificando igual contra el hash dummy** (mismo
   tiempo de respuesta). Un invitado no puede loguearse hasta aceptar.

### 3.1 Política de contraseña única

`_password_invalida` (10 caracteres mínimo, 72 bytes máximo por bcrypt) se mueve de `users_abm.py` a
`core/security.py::password_invalida()` y la usan la ABM, `/reset/{token}` y los tests.

## 4. Correo — `core/infrastructure/mailer.py`

- **stdlib**: `smtplib.SMTP(host, port, timeout=15)` → `starttls(context=ssl.create_default_context())` →
  `login` → `send_message`. Sin dependencia nueva. Wrapper `asend_mail` con `asyncio.to_thread`. Sin reintentos.
- **Settings** (pydantic-settings, prefijo `MONITOR_`): `smtp_host` (`""` = deshabilitado), `smtp_port=587`,
  `smtp_user`, `smtp_password` (**secreto: sólo por env / `.env` del server, nunca en el repo**), `smtp_from`
  (default `smtp_user`), `public_url` (`""` → se usa `request.base_url`; en prod `http://129.80.148.166`).
  `settings.mail_enabled = bool(smtp_host)`.
- **Gmail**: `smtp.gmail.com:587`, contraseña de aplicación (exige 2FA en la cuenta). Tope ~500 mails/día:
  sobra. Salida por 587 desde OCI: **HIPÓTESIS a verificar en el smoke de la Fase 3** (OCI bloquea 25 por
  default; si 587 está cerrado se abre en la security list).
- **Plantillas** (texto plano + HTML mínimo inline, sin imágenes ni recursos externos): reseteo ("Tu link para
  elegir una contraseña nueva") e invitación ("Te invitaron al Monitor Renta Fija AR"). Cuerpo: quién lo
  pidió, botón, vencimiento, "si no lo pediste ignoralo", URL en texto plano.
- **Fallos**: si un envío disparado por el admin falla, el admin ve el error real y recibe igual el link para
  copiar (el token ya existe). En `/forgot` el fallo se loguea WARNING y la respuesta pública no cambia.

## 5. Rutas

### 5.1 Admin (`routers/users_abm.py`, dependencia `get_admin_user_html`)

| Ruta | Qué hace |
|---|---|
| `GET /users` | página: toolbar + tabla + panel lateral. `?u=<id>` precarga la ficha (deep link) |
| `GET /users/{id}/ficha` | fragmento HTMX de la ficha |
| `POST /users/add` | campos `username, full_name, email, notes, is_admin, tabs, access=invite\|password, password`. `invite` no exige email (el link se copia; con email + SMTP además se manda, Fase 3); crea el usuario con `hashed_password="!"`, emite token `invite` y, si `mail_enabled`, lo manda; **siempre** devuelve el link al admin para copiarlo |
| `POST /users/{id}/datos` | `full_name, email, notes`; email duplicado → 400 |
| `POST /users/{id}/permisos` | reemplaza a `/users/update/{id}` (mismos guards del último admin) |
| `POST /users/{id}/reset` | `channel=mail\|link\|manual` (+`password` en manual). `mail` exige email y `mail_enabled`; `link` devuelve el link una sola vez; `manual` = comportamiento actual |
| `POST /users/{id}/sesiones/cerrar` | `token_version += 1` |
| `POST /users/{id}/estado` | `activo=0\|1`. No se puede deshabilitar al último admin ni a uno mismo |
| `POST /users/delete/{id}` | sin cambios |

Las rutas viejas `/users/update/{id}` y `/users/reset-password/{id}` **se eliminan** y sus tests se
actualizan a sabiendas (test_aud_D1 y compañía).

### 5.2 Públicas (`routers/auth.py`)

| Ruta | Qué hace |
|---|---|
| `GET /forgot` | formulario "usuario o email". Si `mail_enabled` es falso, muestra "pedile el link a tu administrador" sin formulario |
| `POST /forgot` | **siempre 200 con la misma página** ("Revisá tu correo"). Si el dato corresponde a un usuario activo con email, emite token `reset/self` y manda el mail **en background** (el tiempo de respuesta no depende del caso). Si no, loguea `forgot=noop` y no hace nada más |
| `GET /reset/{token}` | válido → formulario (nombre del usuario, dos campos, requisitos). Inválido por cualquier motivo → misma página "Este link ya no sirve" (200) |
| `POST /reset/{token}` | valida token y contraseña (las dos iguales + política); consume; **303 a `/login?reset=ok`** (banner verde). Contraseña inválida → 400 con el formulario y el token sigue vivo |

`tests/test_aud_G_tests_route_auth.py::_PUBLIC_PATHS` suma `/forgot` y `/reset/{token}` **a sabiendas**.

### 5.3 Login y sesión

- `POST /login`: el campo "Usuario" acepta **usuario o email** (pedido de David 2026-09-08): se busca
  `username == valor` **o** `email == normalizar_email(valor)`; el rate-limit sigue clavado al valor
  tipeado en minúsculas y `verify_password` corre siempre (con o sin usuario) para no abrir un oráculo.
  Usuario con `is_active=0` → **el mismo mensaje** que credenciales inválidas (no revela estado); en el
  éxito escribe `last_login_at/ip`. `GET /login?reset=ok` muestra el banner.
- El link "¿Olvidaste tu contraseña?" está **siempre** visible (como cualquier sitio); `/forgot` se adapta
  si no hay mail configurado.
- `deps_auth._get_user_from_token`: rechaza `is_active=0` (mata sesiones vivas de un deshabilitado).

### 5.4 Rate-limit

- App (mismo mecanismo en memoria del login): `POST /forgot` 3 por IP / 15 min **y** 3 por dato tipeado / 60 min;
  `POST /reset/{token}` 10 por IP / 15 min.
- nginx (`deploy/nginx/monitores.conf`): `location = /forgot` y `location ~ ^/reset/` con la zona `login`
  existente (clave sólo en POST).

## 6. UI

**Estilo**: `users.html` abandona su CSS "glass" propio y usa los tokens de `app.css` (`--panel-bg`,
`--panel-border`, `--accent`, `--text*`, radios 4/6 px). Las páginas públicas (`login`, `forgot`, `reset`)
extienden un `base_public.html` nuevo: el header de `base.html` **sin nav ni badge** (nada que pegue a rutas
privadas), mismo script de tema, tarjeta centrada `panel-bg`/`panel-border`, botón `--accent`.

**`/users` (Opción A)**
- Toolbar: título, resumen ("N usuarios · activos · invitaciones pendientes · deshabilitados"), búsqueda
  (filtro client-side sobre la tabla), botón "Nuevo usuario".
- Tabla: Usuario (avatar con iniciales, `username · nombre`, email), Rol, Módulos, Último acceso, Estado,
  acciones (reset, más). Clic en la fila → `hx-get` de la ficha al panel derecho + `history.replaceState(?u=)`.
- Ficha: Datos (form → `/datos`), Permisos (form → `/permisos`), Seguridad (botones → POST; mail/link abren el
  diálogo de tres canales), Actividad (derivada, §2.3), footer Guardar / Descartar / Eliminar.
- Nuevo usuario: panel `<details>` como hoy, con los campos nuevos y "Acceso inicial" (invitación por
  defecto; "contraseña a mano" habilita el campo y el generador).
- Diálogo de reseteo: modal `.modal-card` (patrón del `resetPassword` actual) con tres opciones; la de mail
  se deshabilita con motivo visible si falta email o SMTP. Resultado "link": modal con el link seleccionable
  y botón Copiar (`window.mrCopy`, funciona sin HTTPS).
- Canal "a mano" (delta 2026-09-08, pedido de David): el campo de la contraseña del modal es **editable**,
  viene precargado con una generada y tiene botón "Generar otra"; el admin puede tipear la que quiera.
  Validación en el cliente (mínimo 10 caracteres, botón deshabilitado si no cumple) y en el servidor
  (`password_invalida`, como hoy).

## 7. Seguridad

- Token aleatorio de 256 bits, guardado hasheado, **un solo uso**, TTL corto, invalidación de los anteriores
  al emitir uno nuevo, inválido para usuarios deshabilitados; consumirlo cierra las otras sesiones.
- `/forgot` y `/reset` sin oráculo: misma página y mismo tiempo (envío en background) para todos los casos.
- El token viaja en el path: `Referrer-Policy: same-origin` ya está (`security_web.py`); la página no carga
  recursos externos (CSP `'self'`); `autocomplete="new-password"`.
- Email: validación mínima (un `@`, sin espacios ni caracteres de control, ≤ 254, minúsculas) y la misma
  lista de caracteres prohibidos que el username (defensa en profundidad contra XSS almacenado); escape en
  template.
- Auditoría `monitor.audit` (campos saneados con `_limpio`): `reset_link channel=… purpose=… by=… target=…`,
  `reset_consumed`, `forgot=requested|noop`, `user_disabled|enabled`, `sessions_closed`, `user_data_updated`,
  `invite_sent`. Nunca el token, nunca la contraseña.
- **HTTP sin dominio** (decisión de David): el link viaja en claro **igual que hoy viaja la contraseña en cada
  login**: no agrega una clase nueva de riesgo. Mitigaciones: las de arriba. Camino a HTTPS **sin comprar
  dominio**, fuera de este spec y como spike aparte: certificado Let's Encrypt sobre un nombre de DNS
  público tipo `129-80-148-166.sslip.io` con `deploy/setup-https.sh` (HIPÓTESIS: rate limits de LE para ese
  sufijo y dependencia de un DNS de terceros; verificar antes de proponerlo).
- `/security-review` antes de pushear cada fase (agents.md §0.1.13).

## 8. Tests guardianes (TDD; prueba por mutación en los marcados ★)

- Rutas públicas: `_PUBLIC_PATHS` actualizado; el guard "toda ruta exige login" sigue verde.
- `@pytest.mark.noauth` — `/reset/{token}`: token inexistente, vencido, **usado por segunda vez ★**, usuario
  deshabilitado → misma página 200; consumo correcto → hash cambia, `token_version` sube, `used_at` seteado,
  303 a `/login?reset=ok`; contraseña corta → 400 y el token sigue vivo; emitir un token nuevo invalida el
  anterior.
- `@pytest.mark.noauth` — `/forgot`: 200 idéntico para usuario existente / inexistente / sin email /
  deshabilitado; el mailer (stub) se llama sólo en el primer caso; 429 al pasar el límite.
- Login: deshabilitado → mismo mensaje que clave incorrecta **★** (`_get_user_from_token` también rechaza);
  `last_login_at` se actualiza; invitado (`hashed_password="!"`) no entra y no rompe.
- Manager: cada POST exige admin (403 a un usuario común); no deshabilitar al último admin ni a uno mismo;
  email duplicado → 400; `access=invite` crea usuario sin clave + token `invite` y devuelve el link;
  `access=password` conserva el comportamiento actual; `/ficha` devuelve fragmento; `/estado` mata sesiones.
- Mailer: unit test con `smtplib.SMTP` mockeado (STARTTLS con contexto, login, `send_message`, `timeout`
  distinto de `None`).
- Plantillas: el HTML del mail no referencia hosts externos.

## 9. Fases (una por rama/PR, gate verde, `/security-review`, `/compound` al cerrar)

| Fase | Contenido | Útil sin… |
|---|---|---|
| **F1** | Columnas de `users` + `base_public.html` + login restyle + `is_active` (login y sesión) + Manager Opción A completo con canal "a mano", datos, permisos, estado, cerrar sesiones, último acceso; alta con contraseña | tokens ni mail |
| **F2** | `password_reset_tokens` + contrato §3 + `/reset/{token}` + canal "link copiable" + alta por invitación (link copiable) + actividad derivada + `_PUBLIC_PATHS` | mail |
| **F3** | `mailer.py` + settings + canal "mail" + invitación por mail + `/forgot` + link en el login + rate-limits + nginx + docs de despliegue (env SMTP, `public_url`, cómo crear la contraseña de aplicación de Gmail) | — |

**Estado (2026-09-08)**: F1 hecha y deployada. F2 hecha y deployada. F3 hecha (gate verde,
`/security-review` pendiente del controller); deploy a prod pendiente del OK explícito de David
(configurar el `.env` con las credenciales SMTP también queda a mano suya).

Docs que se tocan: `docs/auth.md` y `.claude/rules/auth.md` (rutas públicas, tokens, mailer, `is_active`),
`docs/despliegue.md` (env nuevas), `deploy/nginx/monitores.conf`, `CLAUDE.md` (una línea en **Web**: rutas
públicas = las 5 del test), `docs/decisiones.md` (HTTP sin dominio, Gmail).

## 10. Riesgos y pendientes

- Egress 587 desde OCI (verificar en F3 con un `smoke` que mande un mail real a David).
- Gmail: remitente = la cuenta de David; si algún día molesta, cambiar `smtp_*` a un proveedor transaccional
  no toca código.
- HTTPS vía `sslip.io`: spike aparte antes de proponerlo.
- La fixture autouse `_auth_bypass` corre como admin: todos los tests de rutas públicas llevan `noauth`.
