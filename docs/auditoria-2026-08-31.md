# Auditoría del Monitor de Renta Fija — 2026-08-31

> Revisión multi-agente (8 dimensiones + verificación adversarial, 38 agentes).
> Varios ítems ya se arreglaron en la sesión del 2026-08-31 (ver resumen del chat):
> #5 gate roto, #8 Request no importado, #10 build_options inline, #23 timing de ciclo.

---

# Informe de auditoría — Monitor de Renta Fija

**Resumen en una línea:** el core financiero está sólido y bien testeado, pero la capa de auth de los últimos 3 commits entró a producción sin tests, sin lint, con dos vías independientes de takeover total, y de paso dejó el gate de calidad rojo y ciego desde hace 16 commits.

---

## 🔴 Arreglar ya (riesgo activo en producción)

### 1. Secreto JWT hardcodeado y publicado en GitHub → cualquiera forja un token de admin
**Dónde:** `config/settings.py:101` · `core/security.py:24,29`
**Qué pasa:** `jwt_secret_key: str = "super_secreto_para_desarrollo_cambiar_en_prod"`. Ningún target de deploy setea `MONITOR_JWT_SECRET_KEY` (ni `deploy.sh`, ni el Dockerfile, ni `render.yaml`, ni `vercel.json`; el `.env` solo tiene `BYMADATA_USER/PASS`). Y el repo tiene remoto **público**: `github.com/IronCondorBursatil/MonitorMercadoArgy` (`"private": false`). El commit 556a716 "Anonymize codebase for security" no lo detectó.

Reproducido end-to-end contra la app real (TestClient, DB temporal): anónimo `GET /users` → 302; con token forjado `{"sub":"admin"}` firmado con el string del repo → **200 con el panel de admin** (por header `Authorization: Bearer` *y* por cookie); `POST /users/add` → 200 creando un admin nuevo. El fallback de header en `deps_auth.py:26-28` hace la explotación scriptable y esquiva cualquier defensa basada en SameSite.

**Por qué importa acá:** con `RequireTabPermission.__call__` haciendo short-circuit en `is_admin`, un token forjado abre los 12 routers, el ABM del catálogo (que escribe SQLite, la fuente de verdad, con altas que no existen en ningún otro lado) y el ABM de usuarios. La auth entera es decorativa. Tokens vivos 7 días.

**Arreglo (minutos):**
```bash
# en el droplet, /etc/monitores.env cargado por EnvironmentFile= del unit
MONITOR_JWT_SECRET_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(64))')
```
```python
# config/settings.py — fail-closed, sin default usable
jwt_secret_key: str = ""
@model_validator(mode="after")
def _no_default_secret(self):
    if not self.jwt_secret_key and not os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("MONITOR_JWT_SECRET_KEY no seteado — abortando")
    return self
```
Rotar invalida todas las sesiones (deseable). Después: auditar la tabla `users` del droplet buscando altas no reconocidas — la ventana de exposición incluye tokens de 7 días.

*Nota honesta: la unit de systemd no está versionada, así que no se puede descartar estáticamente un `Environment=` allí. Verificalo con `systemctl cat monitores.service` antes de cerrar el ítem — pero el fail-closed va igual.*

---

### 2. Admin sembrado con `admin`/`admin123` — segunda vía de takeover, independiente del JWT
**Dónde:** `scripts/init_admin.py:21-27`
**Qué pasa:** `UserORM(username="admin", hashed_password=get_password_hash("admin123"), is_admin=True)`. Verificado sobre la DB viva local (`%LOCALAPPDATA%\monitor\catalog.db`, read-only): **un solo usuario, `admin`, `is_admin=1`, y `verify_password("admin123", hash)` devuelve True**. La credencial por defecto está activa hoy, no es hipotética. Y `init_admin.py` es el *único* camino de bootstrap del primer admin (`users_abm.py:11` exige un admin ya autenticado), así que el droplet salió de ahí también.

Agravantes: no hay rate limiting ni lockout en `POST /login` (cero `slowapi`/limiter en el repo), `UserORM` no tiene `must_change_password`, y `admin123` está en el primer tramo de cualquier wordlist — no depende de que el repo sea público.

**Por qué importa acá:** admin no es solo lectura. `users_abm.py` da `/users/add`, `/users/delete/{id}`, `/users/update/{id}` y `/users/reset-password/{id}` (que resetea la clave de cualquiera **sin pedir la anterior**). Quien entra se queda con el sistema.

**Arreglo (minutos):**
1. **Ahora:** rotar la password del admin en prod vía `/users/reset-password`.
2. `init_admin.py`: `pwd = os.environ.get("MONITOR_ADMIN_PASSWORD") or sys.exit("Definí MONITOR_ADMIN_PASSWORD")`.
3. Mientras estás ahí, reemplazar `Base.metadata.create_all(...)` (línea 13) por `init_db()` de `catalog_repository` — el `create_all` pelado no corre `_migrate_table_add_columns` y revienta con `no such column: users.allowed_tabs` justo cuando querés recrear el admin sobre una DB restaurada de un backup viejo.
4. Rate limiting en `/login` (slowapi o contador en memoria por IP+usuario, 5/min).

---

### 3. `/api/health`, `/api/metrics` y `/api/riesgo-pais` quedaron fuera del gating
**Dónde:** `apps/web/app.py:434,446,459`
**Qué pasa:** están declarados con `@app.get(...)` directo sobre `app`, después del bloque de `include_router` (410-430), sin `dependencies=`. Recorriendo el árbol de dependencias de **todas** las rutas, el conjunto abierto es exactamente: `/login` (GET+POST), `/logout` (legítimos) y estos tres. Contraste: `/api/v1/market/snapshot` → 401, `/` → 302.

**Por qué importa acá:** el contenido no es confidencial (precios públicos + analítica derivada; `riesgo-pais` es un re-serve de bondterminal). El problema serio es otro y no está en el reporte original: **`/api/health` devuelve `last_error`** de `AppState.status()` (`state.py:75`), poblado en `app.py:94` con `f"{type(e).__name__}: {e}"` — string crudo de excepción que arrastra URLs internas, nombres de providers y a veces parámetros de query.

**Arreglo (minutos):** mover los tres a un router con `dependencies=api_deps`. Si querés dejar `/api/health` público para probes, recortá el payload no autenticado a `{status, is_stale}` — sacá `last_error`, `last_error_at` e `instruments`.

⚠️ **Rompe tests:** `tests/test_web_app.py:14,27`, `tests/test_health_badge.py:24`, `tests/test_bondterminal_provider.py:179,192` afirman 200 sin auth. Actualizalos en el mismo commit.

---

### 4. `/source/*` (config global + credenciales BYMA) gateado solo por "estar logueado"
**Dónde:** `apps/web/app.py:425` · `apps/web/routers/source.py:88,120`
**Qué pasa:** `include_router(source.router, dependencies=html_deps)` — solo `get_current_user_html`, sin permiso concreto. Sus POST cambian la **fuente de cotizaciones global para todos** (`/source/select`) y escriben/borran las credenciales BYMA en el `.env` del server. Un usuario con cero tabs asignados los ejecuta igual.

**Por qué importa acá:** acabás de construir permisos granulares por pestaña y este router los saltea. Es escalada horizontal sobre configuración global.

**Arreglo (minutos):** `dependencies=[Depends(get_admin_user_html)]`. Son acciones de operación, no de lectura.

---

### 5. El gate de calidad está muerto: 0 de 1449 tests corren desde hace 16 commits
**Dónde:** `tests/test_fci_history.py:112`
**Qué pasa:** `series = {` sin cerrar, con fragmentos de otro test empalmados encima (`return MockResponse(...)` huérfano, `monkeypatch.setattr("httpx.get", fake_get)` con `fake_get` inexistente). pytest **aborta la colección entera**: `1449 tests collected, 1 error` → `Interrupted`. Roto y committeado desde `f442452` (2026-08-29). No hay `pytest.ini`/`pyproject.toml` con `--continue-on-collection-errors` que lo mitigue.

Causa raíz: una edición abortada que migraba el mock de `fh.http_get_json` a `httpx.get`. Se comió el `}`, la cola de `test_net_flow_series_keeps_large_but_plausible_jump` y los headers de `test_record_from_ard_fetches_and_stores` + `fake_get`. El `class MockResponse` huérfano de las líneas 13-19 es la otra mitad. **Dos tests destruidos, no uno.**

**Por qué importa acá:** los 3 commits de auth se escribieron, mergearon y desplegaron sin que corriera un solo test. Y `test_pricing_equivalence.py` — que CLAUDE.md declara invariante sagrado — no se evalúa. (Buena noticia: corrido en aislamiento da **5 passed**. El invariante está sano, solo era inverificable.)

**Arreglo (minutos):**
```bash
git show d6ac472:tests/test_fci_history.py > tests/test_fci_history.py
py -3.12 -m pytest tests/test_fci_history.py -q
```
Después borrar el `MockResponse` huérfano o completar la migración a `httpx.get`.

**Ojo:** arreglar esto NO deja el gate verde. Hay dos paredes más detrás (ítems 6 y 7).

---

## 🟠 Esta semana (deuda que muerde)

### 6. 87 tests rojos: los commits de auth cablearon `RequireTabPermission` sin tocar `tests/`
**Dónde:** `apps/web/app.py:436-447` (12 routers) · `tests/conftest.py`
**Qué pasa:** salteando el archivo roto: **87 failed, 1362 passed**. Cada test de router recibe el HTML del login. Causalidad probada por experimento: overrideando `get_current_user_html` con un admin falso, **87 → 1**. Los 86 son el redirect de login; el 87º (`test_nav_has_on_button_next_to_bonos`) lo rompió `5cf379a` al reescribir el nav de `base.html` a `{% if has_tab(...) %}`, metiendo saltos de línea que rompen un assert de string exacto. Los 3 commits explican los 87, no 86.

Reparto: `test_on_router` 22, `test_abm_router` 13, `test_options_router` 6, `test_catalog_router` 6, `test_bonds_router` 6, `test_source_router` 5, `test_panels_router` 5, `test_modal_a11y` 4, + 10 archivos más. **18 archivos** de la capa web sin red de regresión.

**Arreglo (horas):** fixture en `conftest.py` que overridee `get_current_user`, `get_current_user_html`, `get_admin_user`, `get_admin_user_html` y las 12 instancias de `RequireTabPermission` (recorriendo `route.dependant.dependencies`).

⚠️ **Trampa:** `tests/test_fci_router.py:56` y `tests/test_panels_router.py:494` hacen `app.dependency_overrides.clear()`. Una fixture *session-scoped* queda borrada a mitad de la corrida (da 49 fallos en vez de 1). Hacela **function-scoped**, o convertí esos dos teardowns a `.pop(key, None)` selectivo como ya hacen `test_on_router` y `test_bondterminal_provider`.

---

### 7. `ruff check .` → 51 errores, 48 concentrados en los archivos de auth
**Dónde:** `users_abm.py` (19), `deps_auth.py` (8), `auth.py` (7), `api_v1/stream.py` (3), `init_admin.py` (3), `app.py` (2), `templates.py` (2), `api_v1/market.py` (2), `migrate_users.py` (2), `api/index.py` (2)
**Qué pasa:** 40× W291/W293 (whitespace), 5× F401, 2× E712, 1× F821. Ningún archivo previo a esos commits reporta nada — evidencia directa de que ruff nunca corrió sobre el código nuevo.

**Arreglo (minutos):**
```bash
py -3.12 -m ruff check . --fix   # arregla 44 de 48
```
A mano: los 2 E712 de `users_abm.py:50,83` → `.filter(UserORM.is_admin)`; `api/index.py:21` → `# noqa: F401  # re-export`; y el F821 del ítem siguiente.

---

### 8. `Request` no está importado en `app.py` — bomba de tiempo bajo `from __future__ import annotations`
**Dónde:** `apps/web/app.py:383` (import en la línea 27)
**Qué pasa:** el handler `requires_login_exception_handler(request: Request, ...)` usa `Request` sin importarlo. Sobrevive solo porque la línea 18 tiene `from __future__ import annotations` y Starlette lo invoca posicionalmente. Verificado: `typing.get_type_hints(handler)` → `NameError: name 'Request' is not defined`.

**Por qué importa acá:** ese handler *es* el que redirige a `/login`. El día que alguien saque el `from __future__`, o que un plugin de FastAPI resuelva los type hints, la app no importa y el server no levanta — y el fallo aparece lejísimos de la causa.

**Arreglo (segundos):** `from fastapi import Depends, FastAPI, Request`

---

### 9. `MONITOR_DB_DIR` es una perilla muerta: la DB de prod termina dentro del working tree de git
**Dónde:** `config/settings.py:64-79`
**Qué pasa:** `catalog_db`, `backup_dir`, `price_history_db`, `fci_history_db` e `index_history_db` se evalúan **en el cuerpo de la clase** (`db_dir / "..."`), o sea contra el default estático, no contra el valor que pydantic resuelve del entorno. Verificado ejecutando: setear `MONITOR_DB_DIR` cambia `settings.db_dir` (y crea ese directorio vacío en `model_post_init`) pero **ninguna base se mueve**. `db_dir` no se consume en ningún otro lado del repo (`grep -rn db_dir` → 7 hits, todos en settings.py).

En Linux (sin `LOCALAPPDATA`) el fallback cae en `<repo>/monitor/catalog.db` — la fuente de verdad de usuarios + altas ABM, dentro del árbol de git, sobre el que opera `deploy.sh`.

**Por qué importa acá:** es una **trampa de pérdida de datos silenciosa**. El día que setees `MONITOR_DB_DIR=/var/lib/monitor` en el unit de systemd, vas a ver el directorio creado, asumir que migraste, borrar el viejo, y perder las altas ABM que por diseño no están en el Excel semilla, más las cuentas de usuario.

*Precisiones:* Docker/Render funciona por casualidad (`_BASE_DIR=/app`, el default coincide con la ENV). `deploy.sh` NO ejecuta `git clean -xfd` — ese borrado requiere que lo tipees a mano. Vercel sí está roto, pero es irrelevante (ver Descartado).

**Mitigación inmediata (minutos):** los overrides por campo SÍ funcionan. En el unit de systemd:
```
Environment=MONITOR_CATALOG_DB=/var/lib/monitor/catalog.db
Environment=MONITOR_BACKUP_DIR=/var/lib/monitor/backups
Environment=MONITOR_PRICE_HISTORY_DB=/var/lib/monitor/price_history.db
Environment=MONITOR_FCI_HISTORY_DB=/var/lib/monitor/fci_history.db
Environment=MONITOR_INDEX_HISTORY_DB=/var/lib/monitor/index_history.db
```
(`mkdir -p` + `chown` al usuario del servicio: `model_post_init` solo mkdirea `db_dir`, no el parent real de `catalog_db`.)

**Fix de raíz (horas):** campos `Optional[Path] = None` resueltos en `@model_validator(mode="after")` desde `self.db_dir`, preservando los overrides explícitos que usa `conftest.py:16-23`. Test: `monkeypatch.setenv("MONITOR_DB_DIR", tmp); assert Settings().catalog_db.parent == tmp`.

---

### 10. `build_options` corre inline en el loop de 5s: ~4-5s medidos, sin cache ni gate
**Dónde:** `apps/web/app.py:89-90`
**Qué pasa:** `fetch_options` + `to_thread(build_options, ...)` dentro del `while True`, sin condición ni TTL. Medido contra el panel BYMA **en vivo** (484 filas → 482 válidas, 325 con IV): **5,37s cold / 3,92s warm** en laptop. Por contrato: `iv_implied` ~19-21 valuaciones CRR (no 50 — `pricing.py:101` rompe con `hi - lo < 1e-5`), `compute_greeks` 6 CRR más.

Comparación: el motor de bonos **completo** (243 instrumentos priceables) tarda 0,16s. Las opciones cuestan ~25× más que todos los bonos juntos.

**Por qué importa acá:** `render.yaml` declara `plan: free` con `OMP/OPENBLAS/MKL_NUM_THREADS=1` y `MONITOR_ENGINE_WORKERS=1`. Con ~0,1 CPU de cuota, 4-5 CPU-segundos por ciclo dominan la cadencia entera. El ciclo real no es 5s: es 5 + ingesta + 0,16 + fetch + ~4-5 ≈ 10-11s en dev, y bastante peor en el free tier. Corre aunque nadie tenga `/options` abierto y para usuarios sin el permiso `opciones`.

**Agravante que amplifica:** `ProviderHub._fetch_options_byma` hace `_opt_snapshot.update(merged)` y **nunca poda** → el universo crece con el uptime (ver ítem 11).

**Arreglo (horas):** sacarlo a un `_options_loop(app)` propio con su intervalo (30-60s), como `_bei_loop`. Solo con eso el ciclo de bonos vuelve a 5s reales. Complementos: memoizar por `(ticker, mid, spot, t_days)` con invalidación diaria (fuera de rueda ~70% del tiempo nada cambia); bajar N de 80 a 40 (el árbol es O(N²)).

*Corrección al reporte: el badge stale NO titila — `app_state.update(metrics)` está en `app.py:84`, antes del bloque de opciones, así que la edad nunca supera un ciclo contra el umbral de 30s.*

---

### 11. El snapshot de opciones nunca se purga → el tab abre por defecto en una serie **vencida**
**Dónde:** `core/infrastructure/provider_hub.py:332,334,359-360` · `core/domain/options/expiry.py:48`
**Qué pasa:** `_opt_snapshot`/`_opt_source` solo reciben `.update()`; la purga diaria (`:182-193`) toca únicamente `self._snap` vía `_seen_at`, que nunca contiene símbolos de opciones. Y `expiry.py:48` hace `max(1, (expiry - today).days)`, así que una opción vencida entra con `T=1/365>0` y pasa el guard de `pricing.py:75`.

**La consecuencia principal no es la CPU, es la corrección visible:** `chain.py:203-209` `months_for` ordena por `it.expiry` (la fecha **real**, pasada), y `routers/options.py:75` hace `month = months[0]`. Comprobado ejecutando: call con expiry 2026-08-21 y hoy 2026-08-31 → `t_days=1`, `iv=1.656` (165%), `delta=0.522`, y `months_for(GGAL) → ['AG','OC']`. **La vista por defecto del tab Opciones es la chain muerta**, con IV y griegos fabricados. Se manifiesta al primer vencimiento con el server arriba (semanas), no a los meses.

**Arreglo (horas):** descartar en `build_options` (o al mergear) todo contrato con `expiry < today`, y purgar los vencidos de `_opt_snapshot`/`_opt_source` en el rollover diario que ya existe en `:182-193`. Beneficio lateral: `days_to_expiry` deja de recibir fechas pasadas.

*Sacar "memoria sin cota" del framing: son ~292 bytes/fila, ~1 MB tras miles de contratos.*

---

### 12. Cookie de sesión sin `secure` + sin HSTS + sin revocación (7 días)
**Dónde:** `apps/web/routers/auth.py:29-35`
**Qué pasa:** header emitido literal: `access_token=...; HttpOnly; Max-Age=604800; Path=/; SameSite=lax`. Sin `Secure`. Único middleware: `GZipMiddleware` (`app.py:379`); sin `HTTPSRedirectMiddleware`, sin HSTS, sin `ssl_keyfile` en `run.py`, sin setting de cookie segura.

**Corrección importante al reporte original:** la premisa "producción corre en HTTP puro, 443 cerrado" **no se sostiene** para el deploy verificable. `https://monitor-mercado-argy.onrender.com/login` → 200 con el login de esta app (`x-render-origin-server: uvicorn`, edge Cloudflare); `http://` → 301 a https. Así que **la password NO viaja en claro** y la cookie NO va por HTTP en el tráfico normal. (Si además existe el VPS self-hosted de `deploy.sh` sirviendo en claro, ahí sí aplicaría el encuadre duro — verificalo.)

**El riesgo que queda es real igual:** sin HSTS y sin `Secure`, la primera navegación a cualquier URL `http://` del sitio (tipeada, bookmark viejo, link inducido) adjunta la cookie en claro **antes** del 301. Un atacante on-path se lleva un JWT válido 7 días y **sin ninguna forma de revocarlo**: `_get_user_from_token` (`deps_auth.py:23-40`) solo valida firma y existencia del usuario — no hay `jti`, ni blacklist, ni `token_version`. El logout solo borra la cookie del browser. Y el mismo valor se replaya como `Authorization: Bearer` sin necesitar un browser.

**Arreglo (minutos + horas):**
```python
response.set_cookie(..., secure=settings.cookie_secure, samesite="lax", path="/")
```
con `MONITOR_COOKIE_SECURE` default `True`, `False` solo para `localhost`. Emitir HSTS en el edge. Bajar `jwt_access_token_expire_minutes` de 10080 (7 días) a horas, con refresh. Si querés revocación real: campo `token_version` en `UserORM`, incrementado en logout/reset, validado en cada request.

---

### 13. `requirements.lock` no tiene `passlib`, `bcrypt` ni `PyJWT` — el bootstrap documentado no levanta
**Dónde:** `requirements.lock` (final de las deps de runtime, ~línea 42)
**Qué pasa:** `96be5f4` agregó las tres a `requirements.txt:29-31` pero no al lock. `core/security.py:4-5` las importa a nivel de módulo sin guarda, y `app.py:31 → deps_auth.py:13 → security.py:4` las hace obligatorias. Reproducido: `ModuleNotFoundError: No module named 'passlib'`. Ningún paquete del lock las arrastra transitivamente (`Required-by:` vacío).

**Alcance real (más chico que el reportado):** prod está **bien** — `deploy.sh:16`, `Dockerfile:26`, `render.yaml` y `setup.bat:24` instalan todos `requirements.txt`. El lock es el lado rancio. El único camino roto es copiar literal `pip install -r requirements.lock` de CLAUDE.md:18 / README.md:20 en una máquina limpia.

**Arreglo (minutos):** agregar bajo una sección `# --- auth ---`:
```
passlib==1.7.4
bcrypt==3.2.2
PyJWT==2.13.0
```
Y sumar a `check.ps1` un paso que compare los nombres de paquete de txt vs lock y falle si divergen.

---

### 14. `restore_catalog.py` pisa la DB viva sin validar el backup, y no tiene dry-run
**Dónde:** `scripts/restore_catalog.py:63-64,86` · `core/infrastructure/db/backup.py:100-114`
**Qué pasa:** `--latest` toma ciegamente el último de la lista (`list_backups` solo filtra por glob e `is_file()`: un archivo de 0 bytes entra). No corre `integrity_check`, no verifica que sea una DB SQLite, no cuenta filas en `instruments`/`users`, no pide confirmación. Y `restore_db` **borra los sidecars `-wal`/`-shm` ANTES** de intentar el copy.

**Por qué importa acá:** estado real de esta máquina: `catalog.db` = 1.492 KB, `catalog.db-wal` = **1.536 KB**. El WAL pesa más que la DB — ahí vive trabajo commiteado sin checkpoint. En un restore de emergencia (máximo estrés) podés pisar la DB viva con un backup corrupto y enterarte cuando el server arranca sin instrumentos y sin usuarios.

**Arreglo (horas):** `_validate_backup(path)` que abra el archivo, corra `PRAGMA integrity_check` y cuente `instruments`/`cashflows`/`users`; llamarlo antes de `restore_db`, abortar con exit 4 (con `--force` para override). Sumar `--dry-run` que imprima el resumen y salga sin tocar nada, y prompt de confirmación sin `--yes`. Mover el `unlink` de sidecars a después de un `_online_copy` exitoso a temp + `os.replace`.

---

### 15. Backups en el mismo directorio y disco que la DB, sin copia offsite
**Dónde:** `config/settings.py:69` (`backup_dir = db_dir / "backups"`)
**Qué pasa:** en Linux, DB y sus 7 backups viven ambos en `<repo>/monitor/`. `grep` de `rsync|s3|spaces|scp|cron|offsite` sobre todo el repo → **cero hits**.

**Por qué importa acá:** un solo evento (re-clone del repo para destrabar un pull, `git clean -xfd` manual, pérdida del droplet) se lleva la DB y los 7 backups juntos. Lo irrecuperable son las **altas ABM DB-only** y las cuentas de usuario individuales — el catálogo base sí se recupera (`instruments_master.xlsx` y el CSV de ONs están versionados, `ingest_master.py` re-siembra) y el admin se re-bootstrapea.

**Arreglo (horas):** después de mover las bases fuera del repo (ítem 9), cron que empuje el diario afuera:
```bash
0 4 * * * find /var/lib/monitor/backups -name 'catalog-*.db' -mtime -1 -exec rclone copy {} remote:monitor-backups \;
```
Complementario: en `backup_db`, correr `PRAGMA integrity_check` sobre el archivo recién creado y descartarlo + WARNING si no da `ok` — hoy un snapshot corrupto se guarda y rota igual que uno bueno.

---

### 16. Cero tests de la capa de auth (~800 líneas nuevas en producción)
**Dónde:** `apps/web/deps_auth.py`, `core/security.py`, `apps/web/routers/auth.py`, `users_abm.py`
**Qué pasa:** ningún archivo en `tests/` matchea `auth|user|login|permis|jwt|secur`, y ninguno referencia `RequireTabPermission`, `get_current_user` ni `create_access_token`. CLAUDE.md:138 dice explícitamente "TDD aplica a features nuevas, bugfixes y refactors" — es violación de política documentada, no decisión deliberada.

**Por qué importa acá:** es el único límite entre un anónimo y los datos, y `decode_access_token` (`security.py:27-32`) traga toda `PyJWTError` por igual (expirado, firma inválida, algoritmo distinto). Un refactor futuro ahí abre el acceso sin que nada avise. *Buena noticia: inspeccioné `RequireTabPermission` y no hay bug hoy — `allowed_tabs` es columna JSON (lista real), así que `tab_name in tabs` es membership, no substring; el short-circuit de `is_admin` y el wildcard `"*"` son correctos.*

**Arreglo (días):** `tests/test_auth.py` + `tests/test_permissions.py`, usando la infra del ítem 6 pero **sin** el override:
- Unitarios de `core/security.py`: round-trip hash/verify; token expirado → None; token firmado con otra clave → None; token sin `sub` → None.
- Router: `GET /` sin cookie → 302 a `/login`; `POST /login` con password mala NO setea cookie; login OK setea `access_token` httponly; `/logout` borra la cookie.
- Permisos: `allowed_tabs=['bonos']` → 200 en `/` y redirect en `/fci`; no-admin → 403 en `/users`; admin bypass; wildcard `*`; guard del último admin (`users_abm.py:50-53,82-86`).

---

## 🟡 Cuando puedas

### 17. Bugs financieros de borde (todos minutos, todos con test faltante)

| Bug | Dónde | Qué pasa | Fix |
|---|---|---|---|
| **Doble lag en V.Téc CER del popup** | `bond_detail.py:378` | `ref_date` ya es el settle, pero `calculate_technical_value` recibe `settle_lag` default 1 → `settlement_byma_date` se aplica dos veces. Medido: V.Téc/paridad +0,066% en T+1, **+0,264%** en CI (feriado 17/08). TIR/MD/DV01 no afectados. *No es regresión de v6.1: el motor legacy también hardcodea `lag=1`, por eso `test_pricing_equivalence` no lo detecta.* | pasar `settle_lag=0` |
| **`convexity` sin guard de overflow** | `metrics.py:276-280` | Quedó afuera del fix de v7.2 que sí cubre `vanilla_pv` (`metrics.py:245-252`) y `duration` (`base.py:110-129`). Con TIR ≥ ~1e16 en bono largo → `OverflowError` → **500**. Modo extra no reportado: TIR = `-0.9999999999999999` → `ZeroDivisionError` en la misma línea. Tercer call site: `on_service.py:98` con TIR del solver — ahí tumbaría el panel ON entero. | `try/except (OverflowError, ZeroDivisionError, ValueError): return None` |
| **`duration` devuelve un número complejo** | `strategies.py:202`, `services.py:82-84` | Solo filtra `None`/`NaN`, no acota rango. `TamarStrategy.duration(inst, -2.0, ctx)` → `(0.8013-0.2147j)`, que llega hasta el template (`_safe()` no lo neutraliza: `float(complex)` lanza TypeError). Vanilla → `TypeError` no capturado → 500. | `if tir is None or not np.isfinite(tir) or tir <= -1.0: return None` en `calculate_duration` (cubre las 3 strategies) + `TypeError` al except de `VanillaStrategy` + validar rango de `tir_pct` en `bonds.py:45-50` |
| **Drift de cupones fin-de-mes** | `cashflow_synth.py:193-196` | Itera `cd = cd + relativedelta(months=n)` desde el cupón anterior en vez de anclar en la emisión. `relativedelta` clampea al mes corto y el día se pierde **para siempre**. Emisión 31/08/2024 semestral → 2025-02-28, **2025-08-28**, ... Afecta bonos con cashflow sintetizado: fechas del popup y /cartera, accrued, TIR/MD, y dispara `long_last_coupon` espuriamente | `k=1; while (cd := emision + relativedelta(months=k*n)) <= vto: ...; k+=1` |
| **Cache TAMAR sin identidad de provider** | `tamar.py:64` | key = `(period_start, period_end, forecast_tna)`. `ZeroTamar` (stub que fuerza TAMAR=0 para revaluar un DUAL como tasa fija) y el provider real comparten key. Reproducido: real→0.3, después ZeroTamar→**0.3** (debería ser 0.0). Como el refresh loop puebla la key cada 5s, en prod la pata `_TF` del popup (`GET /bond/TTJ26_TF/detail`) queda silenciosamente sin sentido | incluir `type(indices_provider).__name__` en la key |
| **`cer_base` cae a 1.0 en silencio** | `repositories.py:202` + `instruments_abm.py:227` | `default=1.0` hace el instrumento "CER válido" (truthy) y `CerStrategy` deflacta por el índice CER entero. Reproducido con CER=600: TIR **12.415%**, V.Téc **60.000**. El form ABM no marca `cer emision` como `required` pese a que su help dice "Crítico". Hoy los 37 instrumentos CER/TAMAR tienen base — es trampa latente del ABM | `"required": True` en el form + devolver `None` (no 1.0) cuando falta y el tipo es CER/DUAL_CER_TAMAR |
| **Calendario duplica trasladables de 2027** | `holiday_engine.py:126-136` | El merge deja dos filas por feriado trasladable (fecha efectiva de nager + nominal de argentinadatos) y el filtro solo excluye `xbue_pmc`. Verificado: `es_habil(2027-08-17) == False` (San Martín se trasladó al lunes 16, el 17 es hábil); `settlement_byma_date(2027-08-16, 1)` → 2027-08-18 en vez de 08-17. Ya impacta `_build_anchors` de `cer_return_scenarios` | en `merge_fuentes`, descartar la fila `trasladable` cuando hay otra del mismo feriado/año con más fuentes (el Excel ya trae `N fuentes` / `Solo en 1 fuente`); regenerar con `--refresh` + test anti-consecutivos |

### 18. P&L de cartera: los dos totales suman universos distintos
**Dónde:** `core/domain/portfolio.py:106-107,131,137`
**Qué pasa:** `total_mv` filtra por `market_value_ars`, `total_cost` por `cost_value_ars`, y después se restan. El bug es **bidireccional**:
- Posición **sin** `cost_price` (es opcional: el form dice "Precio costo (opc.)"): su MV entra sin contrapartida → **ganancia inflada**. Repro: pnl 8.700.000 / 9,67% cuando lo real es 7.500.000 / 8,33%.
- Posición **sin precio vivo**: su costo entra sin contrapartida → **pérdida fantasma**. El test existente `test_build_portfolio_missing_price_is_tolerated` usa ese fixture y da `pnl_ars=-50, pnl_pct=-1.0` (pérdida inventada del 100%) — pero solo assertea `total_market_value_ars == 0.0` y nunca mira el P&L.

Alcance: solo la tarjeta de resumen de `/cartera`. El `pnl_pct` por posición (línea 79) está bien, y Escenarios no hereda el bug (`scenarios.py:88-106` deriva de `market_value_ars`).

**Arreglo (minutos):** recorrer una sola vez acumulando mv y cost solo cuando **ambos** están presentes; exponer aparte el MV total para pesos/exposición y un contador de excluidas ("P&L sobre N de M posiciones"). Tests para los tres casos.

### 19. I/O bloqueante en el event loop y contención de locks (4 sitios)

| Sitio | Qué pasa | Fix |
|---|---|---|
| `indices_provider.py:223,245` | `prefetch` es `async` y toma un `threading.Lock` **de clase** desde el event loop. El mismo lock lo retiene `_fetch_all` (`:183`) durante 4 `httpx.get(timeout=10)` secuenciales, llamado desde hilos (`to_thread` de pricing + handlers `def` de `/bcra`, `/header/cards` polleado cada 15s, `/fci/data`). Si un hilo gana la carrera al gate diario, **el loop queda muerto**. Medido: stall de 7,7s con series simuladas de 2s; peor caso ~40s. Ventanas: arranque del proceso y rollover de medianoche AR | `await asyncio.to_thread(...)` alrededor de la toma del lock, o `asyncio.Lock` separado y que `_fetch_all` no retenga el lock durante la red |
| `cafci_provider.py:253-266` | `_ensure_loaded` sostiene el lock de clase durante `httpx.get(timeout=30)` **y** durante `_apply_ard_fallback` (5 GET de 10s) → hold >30s. Peor: el sello `_last_fail_ts` se estampa **después** del fetch, así que los concurrentes no pueden cortocircuitar. `fci_service.py:52` lo llama antes de consultar su propio `_CACHE`. Handler sync → cada request bloqueado retiene un token del threadpool (40) | el repo **ya documenta el patrón correcto** en `bondterminal_provider.py:47-50`: estampar el sello dentro del lock, hacer el GET fuera, publicar con un segundo `with self._lock` |
| `abm.py:239,255` | `abm_save`/`abm_cashflows` son `async def` solo por `await request.form()`, pero después corren transacción SQLAlchemy + `repo.reload()` (relee 552 instruments con `selectin` sobre 1368 cashflows) en el event loop. Medido: **~90ms** por guardado, no "congela el proceso". Contradice CLAUDE.md ("corren en to_thread") y contrasta con `abm_delete` (`:274`), que es `def` pelado y sí va al threadpool | `await run_in_threadpool(abm_store.save_instrument, sheet, fields)` / `await run_in_threadpool(repo.reload)` |
| `abm.py:247,269` | El `except (ValueError, KeyError)` no atrapa `OperationalError: database is locked` (busy_timeout 5000ms). Choca con `_startup_reconcile` (`app.py:165-174`, escribe miles de filas). El operador ve un 500 crudo justo cuando el comentario del propio código dice "NUNCA tragar el error: el operador tiene que saber que NO se guardó" | agregar `SQLAlchemyError` a la tupla con mensaje "la base está ocupada, reintentá" |

### 20. Stampede + envenenamiento de cache en `/fci/data`
**Dónde:** `apps/web/fci_service.py:58-74` · `core/infrastructure/fci_history.py:180-207`
**Qué pasa:** double-checked locking **sin single-flight** — el `_LOCK` cubre solo lectura y escritura del cache; el build queda afuera. `fetch_ard_fci_rows` no cachea nada. Reproducido con 4 threads en frío: 4 builds, **20 GET externos**, 9,21s de wall clock. La clave incluye `str(date.today())` → ventana fría garantizada cada medianoche.

El daño real no es el threadpool (40 tokens, 4 usuarios usan 4): es **contención de GIL**. Cada build son ~1,6s de CPU Python puro — `_store_lookups._matching` es O(3.734 fondos × 4.923 claves) ≈ 10,8M comparaciones de string, invocado dos veces por fondo — en un proceso único que además sirve SSE, el loop de 5s y los 13 paneles SSR.

**Consecuencia adyacente, probablemente peor:** si ArgentinaDatos falla, `fetch_ard_fci_rows` devuelve `[]`, `build_aum_index([])` da índice vacío, y **ese dataset degradado sin AUM se cachea igual** bajo la clave del día. Una caída transitoria de ARD envenena el panel FCI hasta la medianoche siguiente, sin reintento.

**Arreglo (horas):** (a) indexar `store.keys()` una sola vez en un `dict base -> [claves]` para que `_matching` sea O(1); (b) sostener el `_LOCK` durante todo el build (single-flight) para que el 2º request espere en vez de duplicar; (c) TTL de 1h en `fetch_ard_fci_rows` + bajar timeout de 15s a 5s; (d) **no cachear** cuando `aum_index` viene vacío por fallo de ARD.

### 21. Sin poda ni ventana en los dos stores de histórico
**Dónde:** `core/infrastructure/fci_history.py:70-89` · `core/infrastructure/price_history.py:73-92`
**Qué pasa:** ambos hacen `SELECT ... FROM <tabla>` **sin WHERE** y arman el dict completo en RAM de por vida del proceso. Cero `DELETE` en ninguno. (El módulo hermano `byma/index_history.py:86` **sí** tiene `prune()` y lo llama — la convención del repo es podar cuando lo pensaron.)

Medido en las DB reales:

| Store | Hoy | RAM (tracemalloc) | Crecimiento | Ventana que se usa |
|---|---|---|---|---|
| `fci_history` | 16.830 filas / 4.923 fondos / 4 días | 325,5 B por punto | ~4.706 filas/día de uptime → **558 MB/año**, 2,8 GB a 5 años | `hist_axis(n=120, span=364)` y `monthly_net_flows(n=12)` → **12 meses** |
| `price_history` | 138.443 filas / 452 tickers / 8,4 MB | **8,77 MB** | ~50-75k filas/año ≈ +4 MB/año | `fetch_historical_prices(ticker, 400)` → **400 días**. 36,4% del cache (3,2 MB) es peso muerto |

**Prioridad:** `fci_history` es el que importa (crecimiento 100× mayor); `price_history` es higiene pura y sus "20 años" son engañosos (solo 3 tickers tienen datos pre-2020, el 75% es 2025-2026).

**Arreglo (horas):** `WHERE day >= ?` en ambos `_ensure_cache` (hoy − ~450 días, con margen sobre los 364/400) + `DELETE FROM <tabla> WHERE day < ?` en el `_price_history_loop` que ya escribe (`app.py:252`).

### 22. Cero supervisión de las tasks de fondo + shutdown que se rompe
**Dónde:** `apps/web/app.py:357-364` (create_task) y `:367-374` (finally del lifespan)
**Qué pasa:**
- Las 4 tasks se crean sin `add_done_callback`, sin watchdog, sin reintento. `/api/health` mide solo la frescura del snapshot del motor, así que detecta un `_refresh_loop` muerto pero es **ciego** a `_bei_loop` y `_price_history_loop`. El thread WS de Matba lo admite en el log: `"Matba WS thread crashed (won't auto-restart)"`.
- El `finally` solo captura `CancelledError`. Si una task ya murió con excepción almacenada, `task.cancel()` es no-op y `await task` re-lanza — escapando del `for`: las tasks siguientes **nunca se cancelan** y `app.state.client.aclose()` nunca corre (se filtran los pools httpx). Systemd espera el timeout completo antes de matar el proceso.

**Por qué importa acá:** un `_price_history_loop` muerto detiene la acumulación de cierres y de `fci_history` (rendimientos Sem/1M/3M y flujos FCI degradándose en silencio durante días) **y mata el backup diario de catalog.db**, que vive dentro de ese loop (`app.py:261-268`).

**Arreglo (horas):** `add_done_callback` por task que loguee y llame `AppState.record_error`; mejor, un helper `_supervise(coro_factory, name)` que reinicie con backoff. Exponer `bei_last`, `price_history_last`, `matba_ws_connected` en `/api/health`. En el `finally`: `await asyncio.gather(*tasks, return_exceptions=True)` tras cancelar todas, y `aclose()` en su propio `try/finally`.

### 23. Cero instrumentación del tiempo de ciclo — y el fix en vuelo no funciona
**Dónde:** `apps/web/app.py:69-104`
**Qué pasa:** en HEAD no hay una sola medición (`git show HEAD:apps/web/app.py | grep perf_counter` → vacío). **Pero** el working tree tiene `app.py` modificado sin commitear (mtime 2026-08-31 14:26) que agrega `_t0/_t_ingest/_t_price/_t_opts` y un `logger.info("refresh cycle: ...")`.

**Ese fix es un no-op observacional.** `setup_logging()` (`settings.py:169-187`) instala exactamente dos handlers: un `RotatingFileHandler` con `setLevel(WARNING)` (un INFO nunca llega al archivo) y un `StreamHandler` con `_ConsoleFilter` (`:140-165`) cuyo `filter()` termina en `return False` para todo INFO sin `extra={"console": True}`. Y `run.py:37` pasa `log_config=None`, así que uvicorn tampoco agrega handler. **La línea de timing se descarta por completo.**

**Arreglo (minutos):** `logger.info(..., extra={"console": True})` — el docstring del propio `_ConsoleFilter` documenta ese escape hatch. Mejor: guardar el desglose `{'refresh_all': ms, 'pricing': ms, 'options': ms, 'total': ms}` en `AppState` y exponerlo en `/api/health`, que es el canal que ya consultás desde el droplet. Y `await asyncio.sleep(max(0, refresh_sec - elapsed))` para que `refresh_sec` describa la cadencia real y no el período + trabajo.

*Matiz: `age_seconds` de `/api/health` ya es un proxy numérico del tiempo de ciclo (max ≈ `refresh_sec` + duración). Lo que falta es el desglose por etapa y la serie temporal.*

### 24. Memoización del motor de pricing por `(ticker, precio)`
**Dónde:** `core/use_cases/generate_report.py:120-186`
**Qué pasa:** `execute()` repricea los 243 instrumentos cada 5s sin memo por input. TIR (Newton + brentq), V.Téc y MD se recalculan idénticos ciclo tras ciclo. Están bien memoizados el histórico (`_HIST_BASE_CACHE`) y el promedio TAMAR (`_AVG_TAMAR_CACHE`) — falta el pricing en sí.

Profile (cProfile, 5 ciclos): `scipy.optimize.newton` = 54% del ciclo, del cual `numpy.isclose` = **35% del total** (2.083 llamadas/ciclo). `models.year_fraction_to` se llama 4.370 veces/ciclo y cada una re-ejecuta `parse_day_count()` sobre el mismo string (`models.py:141-155`).

El `ThreadPoolExecutor` casi no ayuda: `engine_workers=8` → 0,159s; `=1` → 0,179s. **11%, no 8×** (GIL).

**Por qué importa acá:** fuera de rueda (16h-11h AR + findes + feriados ≈ 70% del tiempo) el resultado es bit a bit idéntico. Son horas de CPU/día quemadas sin cambiar un número, en un droplet que ya está saturado por el ítem 10.

**Arreglo (horas):** memo `(ticker, round(price,6), settle_date, settle_lag, date.today()) → InstrumentMetrics`. Fuera de rueda el ciclo cae a ~0; en rueda solo se repricean los que se movieron. Sirve gratis al camino CI de `panels.py`. Secundario: `@lru_cache` sobre `parse_day_count` (13% del ciclo).

### 25. El toggle CI corre el motor completo dentro del request, sin lock
**Dónde:** `apps/web/routers/panels.py:86-108`
**Qué pasa:** `_ci_metrics` instancia y ejecuta `GenerateMonitorReport` (con su propio pool de 8 threads) dentro de la request. El memo es por `(revision, panel_id)` y la revisión se incrementa **en cada ciclo del refresh loop** → el cache se invalida cada 5s. Read y write del dict sin lock: dos clientes con el mismo panel en CI ejecutan los dos, y la purga itera y borra el mismo dict que otros threads leen (`RuntimeError: dictionary changed size during iteration` → 500 y panel vacío).

**Arreglo (horas):** bloque entero (check + purga + compute + set) bajo `threading.Lock` con double-checked locking. Mejor: computar las métricas CI una vez por ciclo en `_refresh_loop` (el hub ya trae ambos plazos en la misma llamada, sin red extra) y guardarlas en `AppState`, dejando el handler como lectura pura. Con el memo del ítem 24 el costo se vuelve nulo.

### 26. Cada cliente hace ~200 req/min: polling de 15s redundante con el SSE + paneles cerrados que siguen pidiendo
**Dónde:** `apps/web/templates/pages/index.html:80-84,287-290`
**Qué pasa:** los 13 tbody llevan `hx-trigger="load, sse:refresh, every 15s"`. El comentario del template dice que el `every 15s` es "fallback si el SSE se cae", pero htmx **no lo condiciona a nada**: dispara siempre, en paralelo. Y `hidePanel` hace `display:none` conservando el DOM, así que htmx sigue disparando en los paneles cerrados.

Por cliente: 13 req/ciclo por SSE + 52 req/min de polling ≈ 200 req/min, casi todas devolviendo HTML idéntico. Cada una pasa por `RequireTabPermission` → sesión SQLAlchemy nueva + SELECT del user (0,28ms) y toma una conexión del pool (QueuePool size=5, overflow 10): **3-4 clientes en el mismo tick de SSE agotan el pool**.

**Arreglo (minutos):** (a) sacar `every 15s` del `hx-trigger` — htmx-sse ya reconecta solo; si querés red real, polling de 60s activado desde `htmx:sseError`. (b) En `hidePanel`, `item.querySelector('tbody').setAttribute('hx-trigger','none')` y restaurar en `showPanel`. (c) Cachear el user resuelto por token en `_get_user_from_token` con TTL 30-60s, o meter `is_admin`/`allowed_tabs` en el JWT.

### 27. `/fci/data` re-serializa y re-comprime en gzip nivel 9 cada request
**Dónde:** `apps/web/app.py:379` · `apps/web/routers/fci.py:26-27`
**Qué pasa:** `get_fci_dataset` memoiza el **dict**, no la respuesta. Cada GET rehace `json.dumps` sobre el dataset completo y `GZipMiddleware` lo comprime otra vez, sin `compresslevel` (default de Starlette = **9**). Medido sobre 4,1 MB: dumps 32ms, gzip nivel 9 = **107ms** → 0,3 MB; nivel 6 = 43ms → **mismo 0,3 MB**.

**Arreglo (minutos):** ya mismo `GZipMiddleware, minimum_size=1000, compresslevel=6` — mismo tamaño de salida, la mitad de CPU, para **todas** las respuestas de la app. Después: cachear los bytes ya comprimidos junto al dataset con la misma clave de `fci_service._CACHE` y devolver `Response(content=gz, media_type="application/json", headers={"Content-Encoding":"gzip","Vary":"Accept-Encoding"})`.

### 28. `_bei_loop` dispara un `refresh_all()` redundante que corrompe la ventana anti-flicker
**Dónde:** `apps/web/app.py:288` · `core/infrastructure/provider_hub.py:136,168-171`
**Qué pasa:** `_bei_loop` hace su propio `hub.refresh_all()` cada 300s, en paralelo con el del loop principal (y `_startup_reconcile` en el arranque → **3 simultáneos**). `refresh_all` no serializa: muta `_active_sym_counts`/`_active_syms`/`_floor_snaps` **fuera** del `self._lock` (que solo cubre `_seen_at` y `_merge`). Cada corrida del BEI cuenta como un "ciclo" extra que decrementa los contadores de la ventana K=3.

Encima `compute_bei_tables` corre **4** `use_case.execute()` sobre TASA_FIJA/TAMAR/DUAL_TAMAR/CER — los mismos ~50 instrumentos que el refresh loop acaba de pricear y ya están en `AppState.metrics()`.

**Por qué importa acá:** doble carga contra BYMA/Data912 (empuja los breakers hacia `fail_max=4` antes de tiempo) y símbolos que BYMA sí lista salen antes de `_active_syms`, dejando que el floor Data912 los pise con el cierre → **parpadeo de precios en los paneles**.

**Arreglo (minutos):** sacar el `refresh_all()` del BEI loop — el snapshot ya lo mantiene fresco `_refresh_loop` cada 5s. Es exactamente el razonamiento que `_price_history_loop` ya documenta en su comentario de `app.py:206-208`. Y pasarle a `compute_bei_tables` las métricas de `AppState` filtradas por tipo en vez de recalcularlas.

### 29. SSE sin `send_timeout` ni cota de suscriptores
**Dónde:** `apps/web/routers/stream.py:33-34,44`
**Qué pasa:** `EventSourceResponse(event_gen())` sin `send_timeout` (verificado: default de sse-starlette 3.4.4 es `None`). Si el buffer TCP de un cliente se llena, el `yield` bloquea indefinidamente: el generador nunca vuelve a chequear `is_disconnected()` y el waiter de la `asyncio.Condition` de `AppState` queda colgado de por vida. Además el `await request.is_disconnected()` duplica al `_listen_for_disconnect` que sse-starlette ya corre sobre el mismo canal ASGI `receive` (dos consumidores del mismo canal = comportamiento indefinido por spec).

**Arreglo (minutos):** `EventSourceResponse(event_gen(), send_timeout=10)` y **sacar** el `await request.is_disconnected()` — sse-starlette ya cancela el generador al detectar el disconnect.

### 30. Un backup "tagged" bloquea el diario del mismo día
**Dónde:** `core/infrastructure/db/backup.py:71-75`
**Qué pasa:** el chequeo "uno por día" usa `startswith(f"catalog-{stamp}")`, que también matchea los tagged (`catalog-YYYY-MM-DDThhmmss-tag.db`). Si corriste un script destructivo (que crea un tagged) antes del backup diario, el diario de ese día devuelve `None` **para siempre**. Y como los pools rotan por separado (`:90-93`), el tagged compite solo con otros tagged: la actividad de scripts lo desaloja y **el día queda sin ningún snapshot en el pool durable**.

**Por qué importa acá:** justo los días de mayor riesgo (re-seed, ingest de ONs, `pin_*`) son los que pierden su entrada en el pool de 7 días.

**Arreglo (minutos):** excluir tagged del chequeo, reusando el criterio de la rotación:
```python
existing = [p for p in list_backups(bdir) if "T" not in p.stem.split(_PREFIX,1)[-1]]
```
Test: `backup_db(tag='x')` a las 11:00 → `backup_db()` a las 12:00 debe devolver un Path, no None.

### 31. El guard del re-seed detecta bajas pero no sobrescrituras
**Dónde:** `core/infrastructure/db/catalog_repository.py:207-219`
**Qué pasa:** `reseed_with_meta` compara solo conjuntos de tickers (`existing - incoming`). Si el ticker **sí** está en el Excel pero fue editado por ABM, el `delete(CashflowORM) + delete(InstrumentORM)` lo reemplaza por la versión del Excel **sin decir nada**.

**Por qué importa acá:** `app.py:167-171` enriquece ISIN y `ficha_meta` contra BYMA en cada arranque y lo escribe a `instruments` — nada de eso está en el Excel. Un `ingest_master.py` que pasa todos los guards revierte silenciosamente cashflows corregidos a mano, ISIN, ley aplicable y `sector_override`. Ves "Seed OK - 552 instruments" y creés que no perdiste nada.

**Arreglo (horas):** para los tickers en `existing & incoming`, comparar un fingerprint (cantidad y suma de cashflows, isin, raw_fields) y listar los que se sobrescribirían con contenido distinto, exigiendo `--allow-overwrite`. Mínimo viable: **imprimir siempre** el listado de tickers modificados antes de commitear.

### 32. `data/cartera.json` es global: la auth se montó sobre persistencia single-tenant
**Dónde:** `apps/web/cartera_store.py`
**Qué pasa:** acabás de agregar usuarios con permisos por pestaña, pero la cartera es **una sola, compartida** entre todos los que tengan la pestaña habilitada. Cada usuario ve y edita las tenencias de los demás.

**Arreglo (horas):** scopear por `user_id` (columna nueva en una tabla `holdings`, o `cartera_{user_id}.json`). Como mínimo, mientras tanto: restringir la pestaña `cartera` solo a admin y documentarlo.

### 33. Higiene de deploy (agrupado — todo minutos)
- **Dockerfile:** corre como **root** (sin `USER`), sin `HEALTHCHECK`, base sin digest. Agregar `RUN useradd -m -u 1000 -r appuser && chown -R appuser:appuser /app` + `USER appuser`, un `HEALTHCHECK` contra `/api/health`, y pinnear `FROM python:3.12-slim@sha256:...`.
- **`.dockerignore`:** no excluye `frontend/node_modules` (85 MB / 693 archivos), `venv/` (que `deploy.sh:15` crea en la misma raíz — un build en el droplet lo chupa entero, con binarios compilados contra ESE host), `tests/` (3 MB) ni **`data/cartera.json`** (tus tenencias reales viajando dentro de una imagen que puede terminar en un registry). *Nota positiva verificada: `.env` SÍ está excluido y no hay ningún `.db` en el repo.* Agregar esas entradas, o invertir a lista blanca.
- **`run.py:32-45`:** el `while True` que reintenta uvicorn cada 5s le **oculta los crash-loops a systemd**: `systemctl status` dice verde mientras la app crashea en bucle; no se disparan `Restart=` ni `StartLimitBurst`. En prod el `ExecStart` debería ser `venv/bin/uvicorn apps.web.app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='127.0.0.1'` y dejar la supervisión a systemd. *Verificado correcto: `uvicorn.run` va sin `workers=`, o sea 1 solo proceso — no hay N refresh loops paralelos. Si algún día agregás workers, hay que sacar los loops del lifespan a un proceso aparte.*
- **`deploy.sh`:** sin `set -e` — puede imprimir "Despliegue completado" sin haber desplegado nada. Sin backup ni rollback. Agregar `set -euo pipefail` y un `curl -f localhost:8000/api/health` post-restart.
- **La definición de producción no está versionada:** `grep -ril "systemd|nginx|gunicorn|certbot"` devuelve **solo `deploy.sh`**. El unit, el `EnvironmentFile`, el `User=`, el `proxy_pass` y los headers viven únicamente en el disco del droplet. Si se pierde, reconstruir es arqueología — y no podés revisar en code review si uvicorn corre con `--proxy-headers` (necesario para que `request.url.scheme` sea https cuando pongas TLS) o si el servicio corre como root. **Crear `deploy/monitores.service` y `deploy/nginx-monitores.conf`** con el contenido real (`systemctl cat monitores.service`), sanitizando secretos.
- **Tres targets conviviendo:** `vercel.json` + `api/index.py` (estructuralmente inviable, ver Descartado), `render.yaml` (con `healthCheckPath: /`, que desde los commits de auth devuelve **302 a `/login`**, no 200) y `deploy.sh` (el real). Borrar Vercel; decidir sobre Render (si se conserva, `healthCheckPath: /api/health`); documentar el target real en un bloque "Despliegue" de CLAUDE.md.

### 34. Scaffold React sin terminar: `/react` 404 y `api_v1/` sin consumidor ni tests
**Dónde:** `apps/web/app.py:400-402` · `deploy.sh` · `frontend/`
**Qué pasa:** el mount de `/react` es condicional a `react_build_dir.exists()`, pero `frontend/dist` está gitignoreado y `deploy.sh` nunca corre `npm build`. En prod el 404 vuelve como `{"detail":"Not Found"}` con `Content-Type: application/json` — es el 404 de FastAPI, o sea el request llega y **el mount no se registró**. Ningún template linkea a `/react` (`git grep "/react"` → 0 hits), así que ningún usuario choca con nada roto.

**El fix obvio no alcanza:** `frontend/vite.config.ts` no define `base`, así que el build emite rutas absolutas de raíz (`/assets/index-*.js`). Servido bajo `/react`, el `index.html` cargaría pero los assets pegarían contra `/assets/...`, que no sirve nadie → SPA en blanco. Hace falta `base: '/react/'` **además** del build en el deploy.

Costo real: 144 líneas TSX + 101 líneas de `api_v1/`, mantenidas en cada arranque, más 85 MB de `node_modules` **dentro de OneDrive** — exactamente la clase de problema que motivó el invariante "nada de venv dentro del proyecto" de CLAUDE.md, que es anterior al scaffold React. `api_v1/market.py:7` importa `_row_values` (privada) de `panels_rows` y `grep api_v1 tests/` → 0: el próximo que toque `panels_rows` rompe `market.py` sin enterarse.

**Arreglo (horas):** **Opción A (recomendada):** borrar `frontend/`, `apps/web/routers/api_v1/`, el mount de `app.py:390-402` y los imports de `:40-41,429-430`. Recuperás 85 MB de OneDrive. **Opción B:** documentarlo como WIP, agregar `base: '/react/'` + `npm ci && npm run build` al deploy, poner el mount detrás de auth (`app.mount` **no** hereda las `dependencies` de los routers), extender el invariante de CLAUDE.md a `node_modules`, y romper el acoplamiento a `_row_values` con un test de `build_market_json`.

### 35. CSRF, enumeración por timing, exposición de passwords generadas
Todos de severidad baja, agrupados porque comparten origen:
- **CSRF:** ningún form lleva token. Hoy `SameSite=Lax` es la **única** línea de defensa (funciona), pero `/logout` es **GET** y state-changing, que Lax sí permite cross-site → logout-CSRF. **Fix:** convertir `/logout` en POST; si alguna vez relajás a `SameSite=None`, todos los POST quedan expuestos.
- **Enumeración por timing** (`auth.py:21`): `not user or not verify_password(...)` corto-circuita, así que un usuario inexistente responde instantáneo y uno existente paga ~200-300ms de bcrypt. **Fix:** verificar siempre contra un hash bcrypt dummy fijo cuando el usuario no existe.
- **Password generada expuesta** (`users.html:434-458`): el generador JS usa correctamente `crypto.getRandomValues` (entropía OK, el sesgo de módulo `Uint32 % 71` es despreciable). El problema es exposición: `input.type="text"` por 5s, `confirm()` con la clave en claro, portapapeles. **Fix:** generar server-side y no reflejarla en el DOM más de lo imprescindible.

### 36. `core/holiday_engine.py`: el dominio puro importa openpyxl y httpx
**Dónde:** `core/holiday_engine.py:43-49` · `core/domain/conventions.py:16`
**Qué pasa:** 945 líneas, 31 funciones top-level, tres responsabilidades: (a) motor de días hábiles/settlement usado por todo el pricing, (b) ETL que descarga feriados de 5 fuentes por HTTP, (c) generador de reporte Excel con estilos. Como todo vive en un módulo, `conventions.py` —la capa de convenciones puras— importa a top-level un archivo que arrastra `openpyxl`, `httpx`, `pandas` y `pandas_market_calendars`.

**Por qué importa acá:** es el caso donde dividir **sí** se justifica (a diferencia de `bond_detail.py`/`instruments_abm.py`/`panels_rows.py`, que son grandes pero cohesivos). Importar un day-count carga openpyxl y abre la puerta a I/O de red desde el dominio; `openpyxl` está declarado en `requirements.txt` como "solo seeding" cuando el pricing lo necesita para importar; y los 8 consumidores del motor no pueden testear el calendario sin el stack de Excel/HTTP.

**Arreglo (días):** partir en tres conservando `core/holiday_engine.py` como fachada que re-exporta (no rompe los 8 consumidores): `core/domain/calendar.py` (motor puro), `core/infrastructure/holidays_etl.py` (fetch_*/merge/cache), `scripts/export_feriados_xlsx.py` (generar_excel). **Hacerlo con el gate verde primero** — sin la suite corriendo (ítems 5 y 6) este refactor es a ciegas.

### 37. CLAUDE.md no menciona una sola palabra de auth, permisos, `api_v1`, `frontend/` ni deploy
**Qué pasa:** el proyecto trata CLAUDE.md como contrato vinculante (dice literalmente que sus instrucciones ganan sobre las skills), pero documenta una app **sin autenticación**. Quien lo lea —vos en tres meses, o el próximo agente— va a montar el próximo router sin protección.

Además `agents.md` declara vigentes **dos convenciones que el código ya no implementa**:
- `agents.md:243-244` dice "rail TAMAR **diario**: `(1 + (TAMAR_d + spread)/365)`". El código capitaliza **mensualmente** (`conventions.py:23-31`, `_TAMAR_K = 365/32`; `tamar.py:145-149`, `max(tem_tamar, floor_rate_monthly)` y `payoff = 100*(1+tem_max)**n_months`). El docstring de `tamar.py` dice que ESA es la fórmula oficial BONTE TAMAR validada contra la referencia para TTJ26 → **manda el código**.
- `agents.md:371-373` dice "**T+0** para LECAP/BONCAP/LECER". El código ignora el tipo: `conventions.py:104-107` → `lag = 1` con el comentario "la convención actual es lag=1 para todos" (el motor legacy congelado hace lo mismo, o sea el cambio es anterior al refactor).
- Menor: `agents.md:245` dice "CER se proyecta linealmente"; `tamar.project_cer_at` es explícitamente **compuesta**.

**Por qué importa acá:** reescribir el rail TAMAR en base diaria rompe la calibración de referencia de TTJ26; aplicar T+0 a LECAP mueve el descuento un día hábil (≈0,08% a 2,5% mensual) y rompe `test_pricing_equivalence`. Es la trampa más cara del repo porque el doc *parece* autoritativo.

**Arreglo (minutos):** actualizar las tres líneas de `agents.md` a lo que hace el código (referenciando `conventions.py` y `tamar.py` como fuente) o marcarlas como **HISTÓRICAS**, igual que ya se hizo con la sección web. Y agregar a CLAUDE.md un bloque de auth/permisos y uno de despliegue.

---

## ⚪ Descartado (no lo revises de nuevo)

- **`frontend/node_modules` y `dist` trackeados en git** — falso: `frontend/.gitignore` los cubre, `git ls-files frontend` = 20 archivos de código, repo sano en 96 KiB empaquetados. El problema es solo la sincronización de OneDrive (ítem 34).
- **`test_pricing_equivalence.py` fallando por TZX26** — no reproduce con la fecha de referencia actual: **5 passed** en aislamiento. El invariante del motor está sano, solo era inverificable por el ítem 5.
- **Bug de permisos en `RequireTabPermission`** — busqué específicamente y no existe: `allowed_tabs` es columna JSON (lista real), así que `tab_name in tabs` es membership de lista, no substring (el clásico `"on" ⊂ "bonos"` no aplica). Short-circuit de `is_admin` y wildcard `"*"` correctos.
- **"El Dockerfile/Render está roto por `MONITOR_DB_DIR`"** — funciona por casualidad: en el contenedor `_BASE_DIR = /app`, así que el fallback da `/app/monitor`, exactamente lo que la ENV pretendía.
- **"`deploy.sh` borra la DB con `git clean -xfd`"** — el script solo hace `git pull` + `pip install` + `systemctl restart`. `git pull` nunca borra ignorados; ese borrado requiere que lo tipees a mano.
- **"El badge de staleness titila por el costo de opciones"** — no: `app_state.update(metrics)` está en `app.py:84`, **antes** del bloque de opciones, así que la edad del snapshot nunca supera un ciclo contra el umbral de 30s.
- **"Arreglar Vercel"** — el modelo serverless es estructuralmente incompatible con esta app (3 loops infinitos en el lifespan, SQLite como fuente de verdad, FS de solo lectura). El propio `api/index.py:4-5` lo admite en su docstring. Arreglar el `MONITOR_DB_DIR` no produce un deploy Vercel funcional. Borralo, no lo arregles.
- **"El threadpool de anyio se agota con 3-4 usuarios"** — el limiter default es **40 tokens** (verificado en runtime) y uvicorn corre 1 solo worker. 4 usuarios usan 4/40. El daño real en `/fci/data` es contención de GIL, no agotamiento de tokens.
- **"`/fci/data` es un vector de DoS anónimo"** — está detrás de `RequireTabPermission("fci")`, y `fci.js:660` hace **un** fetch por carga de página, sin retry ni polling. Sumado al límite de ~6 conexiones por origen de HTTP/1.1, un browser no puede saturar nada.
- **"El backup diario bloquea el ABM 5 segundos"** — `backup_db` es un `sqlite3.backup` que **lee** catalog.db y escribe a otro archivo; bajo WAL los lectores no bloquean escritores. El único escritor competidor real es el ingest BYMA del arranque (`byma/universe.py:84`).
- **"`abm_save` congela el proceso entero"** — medido sobre copia de la DB real: **~90ms** (save 14-52ms + reload 36-74ms). No corta los SSE (`_PING_TIMEOUT_S = 15.0`). Sigue siendo higiene que vale arreglar (ítem 19), pero no es disponibilidad.
- **"`build_options` tarda 10,7s"** — ese número es una chain sintética de 960 contratos todos con precio (cota superior). El panel real da **3,9-5,4s**. Y `iv_implied` hace ~19-21 iteraciones, no 50 (`pricing.py:101` rompe con `hi - lo < 1e-5`).
- **"El doble lag de V.Téc es una regresión del fix de CHANGELOG v6.1"** — no: el motor legacy congelado (`tests/_legacy_engine.py:387`) también hardcodea `lag=1`. Es una inconsistencia latente de siempre, y por eso el test de equivalencia no puede detectarla.
- **"`price_history` carga 20 años y usa 25-30 MB"** — medido con tracemalloc: **8,77 MB**, y solo 3 tickers tienen datos pre-2020 (6% del total). Sigue faltando el `prune` (ítem 21), pero es higiene, no urgencia.
- **"El SPA React roto le rompe la experiencia a algún usuario"** — ningún template linkea a `/react` (`grep` → 0 hits), así que nadie encuentra el 404. Es WIP sin terminar, no un deploy roto.

---

## Lo que está bien

No es cortesía: estas cosas son las que hacen que los hallazgos de arriba sean *arreglables* en vez de un rewrite.

**El núcleo de pricing tiene una red real y bien pensada.** `test_pricing_equivalence.py` compara el motor nuevo contra el original **congelado** (`tests/_legacy_engine.py`) sobre todos los instrumentos — cuando lo corrí en aislamiento pasó los 5 tests. Es la clase de invariante que la mayoría de los proyectos declara y nadie implementa. Sumado a los golden tests contra la referencia, cubre las convenciones raras que son donde se pierde plata: LECAP 30/360, MD BYMA con `m=freq`, TAMAR con `m=12`, pares NT8/2024.

**La arquitectura de pricing post-reingeniería está limpia de verdad.** La tabla predicado→strategy de `registry.py` mató la escalera if/elif, `PricingContext` es inmutable, `VanillaStrategy` + `super()` fallback evita la duplicación entre CER/DL/TAMAR/Dual, y `FinancialEngine` preserva firmas como fachada delgada. Eso es lo que permite que el fix del doble lag (ítem 17) sea una palabra en vez de una cirugía.

**El diseño macro de robustez del runtime es correcto.** Los tres loops tienen `try/except` con `CancelledError` re-lanzado, el hub preserva el snapshot stale ante fetch fallido, el circuit breaker degrada en vez de tumbar, `AppState` publica referencias atómicas con `revision`/`wait_for_change` para el SSE, y los stores SQLite usan conexión efímera + WAL + `busy_timeout`. Cuando algo falla, la app sigue sirviendo el último snapshot bueno — que es exactamente el comportamiento que querés en un monitor de mercado.

**El invariante forward-only del schema se sostiene bajo verificación.** Cero `drop_all`/`DROP TABLE` en código de producción (grep limpio). La `catalog.db` real pasa `integrity_check` y `foreign_key_check` sin huérfanos. Y los guards de `ingest_master` (aborta con el server vivo, aborta si borraría altas DB-only, backup pre-op incondicional) **realmente abortan** — los probé.

**La verificación TLS por host** (`core/infrastructure/_tls.py`) es la solución correcta al problema difícil: verifica por defecto y saltea solo los hosts con cadena rota verificados en vivo, en vez del `verify=False` global que hace todo el mundo.

**El repo se corrige a sí mismo cuando alguien piensa el problema.** `bondterminal_provider.py:47-50` documenta explícitamente el anti-patrón "HTTP bajo el lock" y por qué se sacó — la solución para CAFCI (ítem 19) ya está escrita en el propio codebase. `byma/index_history.py:86` tiene el `prune()` que a `fci_history`/`price_history` les falta. `abm_delete` es `def` pelado y va al threadpool correctamente, a diferencia de sus dos hermanos `async`. Los patrones buenos existen; lo que falta es aplicarlos consistentemente.

**Y el memoizado de bordes está bien elegido:** `_HIST_BASE_CACHE` por `(ticker, día)`, `_AVG_TAMAR_CACHE` con invalidación diaria, `fci_service` memoizado por corte. Alguien pensó dónde ponía los caches. Solo falta el del pricing en sí (ítem 24).

**Cierre:** de los 23 hallazgos confirmados, **5 se arreglan en minutos y son los 5 que más importan** (secreto JWT, admin123, endpoints abiertos, `/source/*`, el SyntaxError del gate). Empezá por ahí — un `git show d6ac472:tests/test_fci_history.py > tests/test_fci_history.py` y cinco líneas de config te devuelven la red de seguridad y te sacan del takeover remoto la misma tarde.