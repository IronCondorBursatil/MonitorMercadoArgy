# Despliegue y operación

Movido desde `CLAUDE.md` en la Fase 4 y **corregido** (agents.md §0.2 / §0.6). Regla "un
hecho, un archivo" (§0.1.4): lo que se hace EN la caja —host, paths, systemd, nginx,
sudoers, backups offsite, diagnóstico, scripts manuales— vive en `deploy/README-ops.md`;
acá va el flujo de deploy, lo que hay que setear y las decisiones de robustez que afectan
a producción. No repetir lo uno en lo otro.

## Producción

- **Oracle OCI**, Ampere A1 **ARM aarch64**, Ubuntu 24.04, Python 3.12.3. Usuario, paths
  del repo y de los datos, systemd y nginx: `deploy/README-ops.md`. IP 129.80.148.166,
  nginx :80 → uvicorn 127.0.0.1:8000, **HTTP sin TLS** (no hay dominio). Alias ssh
  `monitor-oci`; el alias viejo `monitor-do` apunta al MISMO host desde 2026-09-07. La VM
  anterior (otro proveedor) está apagada: cualquier doc, memoria o comentario viejo que la
  nombre se lee "servidor Oracle" (agents.md §0.2).
- Es el ÚNICO target: Render, Vercel y el `Dockerfile` se dieron de baja (2026-08-31).
- El servicio es `systemd monitores.service` con venv creado por `deploy.sh` (gitignoreado);
  en el servidor los scripts se invocan con `venv/bin/python`, no con `python3`.
- `MONITOR_DB_DIR=/var/lib/monitor` vive en el drop-in de systemd (+ `/etc/profile.d/monitor.sh`
  para shells de login). **Una shell manual o `ssh host 'cmd'` NO lo hereda**: todo script a
  mano lleva `MONITOR_DB_DIR=... venv/bin/python scripts/x.py` (README-ops › Scripts manuales).

## `deploy.sh` — el único camino a producción

`ssh monitor-oci`, `cd` al repo, `bash deploy.sh [--upgrade]`. Hace, en orden y con
`set -euo pipefail` (si algo falla antes del restart, el servicio viejo sigue arriba):

1. `git pull origin main`. **Ojo**: ese pull también actualiza `deploy.sh` en disco, pero bash
   ya leyó el script y termina la corrida con la versión VIEJA (2026-09-07: el primer deploy
   tras agregar los freezes no los escribió; el segundo sí). Un cambio en `deploy.sh` aplica
   en el deploy siguiente — si tiene que aplicar ya, correrlo dos veces.
2. Valida o crea el venv **3.12** y aborta si el intérprete es otra minor: `run.py` exige
   3.12.x, y un venv de otra versión instala todo bien y recién revienta en el healthcheck.
3. `pip freeze` ANTES → `${MONITOR_DB_DIR:-/var/lib/monitor}/freeze/freeze-<stamp>-antes.txt`.
   Nunca `$MONITOR_DB_DIR` pelado: con `set -u` y una shell que no lo hereda, abortaría acá.
4. `pip install -r requirements.txt` (qué instala cada entorno y por qué el lock es solo de la
   laptop: `agents.md §0.3`). Con `--upgrade` resuelve dentro de las cotas del `.txt` (política
   A de `docs/decisiones.md` D2, pendiente de David); sin el flag, prod sigue siendo la foto
   del último rebuild del venv.
5. `pip freeze` DESPUÉS → `...-despues.txt`. Rollback de versiones = `pip install -r <antes>`.
6. `systemctl restart` + healthcheck `/api/health`.

No corre tests, migraciones ni instala configuración del sistema (eso es
`deploy/bin/install-config.sh` con root; README-ops). El skill `/deploy` (solo lo dispara el
usuario, con OK previo de David) encadena: `main` limpio → CI verde (`gh run watch
--exit-status`) → ssh `deploy.sh` → salud remota → diff de freezes. Baseline versionado del
freeze de prod: `deploy/freeze/prod-2026-09-07.txt`.

## Lo que hay que setear en prod

- `MONITOR_JWT_SECRET_KEY` — si no, se genera y persiste en `db_dir/jwt_secret` (0600).
  Rotación: `docs/decisiones.md` D3.
- `MONITOR_DB_DIR` fuera del working tree. **Alcanza con esa sola variable**: `db_dir`
  reubica TODO el conjunto (`catalog.db`, `backups/`, `history/`, y los 4 stores
  `price_history`/`fci_history`/`ratings_history`/`index_history`), porque los paths
  derivados se resuelven en `model_post_init` a partir de `_DB_DERIVED`. Los overrides por
  campo (`MONITOR_CATALOG_DB`, `MONITOR_BACKUP_DIR`, …) siguen existiendo y **ganan**, pero
  ya no son obligatorios, y agregar un store nuevo no obliga a tocar la receta de deploy.
  Default en Linux: `$XDG_DATA_HOME/monitor` o `~/.local/share/monitor`, con UNA excepción
  deliberada: si ya existe `<repo>/monitor/catalog.db` se lo respeta, porque mudarlo en
  silencio arrancaría con el catálogo vacío y dejaría la base viva huérfana. Ese caso lo
  denuncia `Settings._check_db_paths` con un ERROR por arranque (`MONITOR_DB_IN_TREE_FATAL=1`
  para que sea fatal en vez de log) — hay que mover las bases y setear `MONITOR_DB_DIR`.
  En Windows el default es `%LOCALAPPDATA%\monitor`.
- **TLS / cookie**: hoy prod sirve por HTTP (nginx catch-all sin `server_name`; Let's Encrypt
  no emite para IP desnuda), así que `cookie_secure` queda en `False` **a propósito**:
  activarlo sin HTTPS hace que el browser descarte la cookie de sesión → login en loop.
  Cuando haya dominio con A a la IP: `bash deploy/setup-https.sh <dominio> <email>` (como
  root) y recién entonces `MONITOR_COOKIE_SECURE=true`. **NO** hace falta agregarle
  `--proxy-headers --forwarded-allow-ips` a uvicorn: desde 0.48 `proxy_headers=True` y
  `forwarded_allow_ips="127.0.0.1"` son default (verificado con `inspect.signature` sobre
  `uvicorn.Config`), así que `request.url.scheme` sigue el `X-Forwarded-Proto` de nginx.
- `MONITOR_TRUSTED_PROXY_IPS` solo si el proxy no es local (`docs/auth.md › Rate-limit`).
- **Correo saliente (Manager: reseteo por mail, invitaciones, «¿Olvidaste tu contraseña?»)**:
  `MONITOR_SMTP_HOST=smtp.gmail.com`, `MONITOR_SMTP_PORT=587`, `MONITOR_SMTP_USER=<tu cuenta gmail>`,
  `MONITOR_SMTP_PASSWORD=<contraseña de aplicación>`, opcional `MONITOR_SMTP_FROM`, y
  **`MONITOR_PUBLIC_URL=http://129.80.148.166`, obligatoria para el correo**: sin ella el correo queda
  apagado y el arranque lo denuncia por ERROR; los links que van por mail se arman sólo con esa base
  — nunca con el Host del request, que cualquiera puede falsear en un POST anónimo a `/forgot`
  (`reset_service.mail_link`). Van en el `.env` del servidor (mismo lugar que las credenciales BYMA),
  nunca en el repo. Host o `PUBLIC_URL` vacíos = correo apagado: el Manager muestra «Enviar link por
  mail» deshabilitado con el motivo en el `title` y `/forgot` avisa que pidan el link al
  administrador. **Contraseña de aplicación de Gmail**: la cuenta necesita verificación en 2 pasos;
  después en `myaccount.google.com › Seguridad › Contraseñas de aplicaciones` se crea una para
  «Monitor» (16 caracteres, se muestra una sola vez). Verificar egress a 587 desde OCI en el primer
  deploy (`ssh monitor-oci 'timeout 5 bash -c "</dev/tcp/smtp.gmail.com/587" && echo abierto'`); si
  está cerrado, abrir la regla de salida en la security list.

## Primer arranque (laptop o servidor nuevo) — la única receta

La app entera está detrás de login y NO hay bootstrap automático de usuarios: ni el
lifespan ni `deploy.sh` tocan `UserORM`. Con un `db_dir` virgen, `/login` renderiza bien y
responde "Usuario o contraseña incorrectos" para siempre — un callejón silencioso, no un
500. ANTES del primer `run.py`:

```
# laptop (PowerShell)
$env:MONITOR_ADMIN_PASSWORD='...'; py -3.12 scripts/init_admin.py
# servidor (MONITOR_DB_DIR explícito: la shell no lo hereda)
MONITOR_DB_DIR=/var/lib/monitor MONITOR_ADMIN_PASSWORD='...' venv/bin/python scripts/init_admin.py
```

En la máquina del autor no se nota: la `catalog.db` vive fuera del árbol, así que un
`git clone` fresco reusa el admin que ya existe. Los usuarios siguientes se crean desde el
ABM de usuarios (`/users/*`, admin).

## Backup del catálogo

`backup_db` (`core/infrastructure/db/backup.py`) toma un snapshot online (consistente con
WAL) 1×/día al arrancar y en cada vuelta horaria del `_price_history_loop`, rotando a
`settings.backup_keep` (7) en `<db_dir>/backups`. El alta automática de letras corre
DESPUÉS de ese backup: toda escritura automática queda precedida por una copia. Restore:
`scripts/restore_catalog.py`. Lo que `backup_db` no cubre (los 4 históricos, `jwt_secret`,
`.env`, cartera, layout) lo junta `scripts/backup_bundle.py` por timer de systemd —
instalación, simulacro y lo pendiente (destino offsite) en `deploy/README-ops.md › Backups`.

## Verificación TLS por host

`core/infrastructure/_tls.py`: se **verifica siempre** y la allowlist de excepciones
arranca **VACÍA**. Hasta 2026-09 exceptuaba `open.` y `addin.bymadata.com.ar` por una
cadena rota observada en 2026-06; re-verificado EN VIVO el 2026-09-03 con trust store
**certifi-only** (el que usa httpx en Linux, sin el store del SO), los tres hosts BYMA
encadenan bien contra GlobalSign RSA OV SSL CA 2018 — la excepción quedó obsoleta y
mantenerla era degradar dos hosts que ya no lo necesitan. Si alguna cadena vuelve a
romperse, la perilla es el env `MONITOR_TLS_NO_VERIFY_HOSTS` (CSV de hosts); NO volver a
poner `verify=False` en un cliente, que deja ese override inerte. El agente tampoco puede
saltearla: `curl -k` y `-SkipCertificateCheck` están en `deny` (`.claude/settings.json`).

## Zona horaria

`settings.timezone` (default `America/Argentina/Buenos_Aires`): `apply_timezone()` corre
al importar `config/settings.py` y fija la TZ del proceso (`TZ` + `time.tzset()`). El
servidor corre en Etc/UTC y la app usa `datetime.now()`/`date.today()` naive: sin esto el
header mostraba UTC y, peor, entre las 21:00 y las 24:00 ART el "hoy" del dominio
(settlement, cashflows) ya era el día siguiente. **No-op en Windows a propósito**: el CRT
de MSVC no parsea nombres IANA y cae a UTC (adelantaba 3hs la hora local en desarrollo);
allá la TZ del SO ya es la correcta. `last_refresh` de `/api/health` viene en ART naive:
para medir frescura comparar `age_seconds`, no timestamps.

## Gate, CI y vigilancia

- **Local**: `pwsh scripts/check.ps1` (ruff + pytest; `-Fast` = `-x`) o el skill `/gate`.
  Los skips se asertan por MOTIVO esperado por plataforma (`tests/_skip_guard.py`), no por
  cuenta: un skip por falta de `node` es rojo.
- **Pre-push**: `pwsh scripts/install-hooks.ps1` instala `.git/hooks/pre-push` (finales LF,
  `-Fast` opcional, `-Remove` desinstala) que corre el gate y aborta el push si está rojo.
  Una vez por clon: `.git/hooks` no viaja con el repo.
- **CI**: `.github/workflows/gate.yml` corre `scripts/check.sh` en `ubuntu-latest` y
  `ubuntu-24.04-arm` (la arquitectura de prod) en cada push, instalando `requirements.txt`
  (abierto, a propósito: caza el drift antes que prod) y publicando el freeze como artifact;
  `pip-audit` como step `continue-on-error`. ~2,7 min. `deps-refresh.yml` (semanal) resuelve
  el `.txt` fresco en ambas patas; el skill `/deps-refresh` baja ese freeze y regenera el
  lock con `scripts/relock.py`.
- **Staleness**: `staleness.yml` hace GET a `/api/health` de prod cada hora y falla (email
  de GitHub, `docs/decisiones.md` D4) si `is_stale`, `status != "ok"` o
  `loop_crashes_24h > 0`. Los `schedule` de GitHub corren con demora y se desactivan solos
  tras 60 días sin actividad en el repo.
- **Smoke / UI**: `/smoke` (127.0.0.1:8001, sin tocar el :8000, limpia el puerto) y
  `/verificar-ui` (Playwright MCP, logueado, consola sin errores).

## Secretos

Credenciales BYMA del usuario en `.env` (gitignored; el agente tiene `Read(./.env)` en
`deny`; se cargan desde la UI de `/source/*` y aplican en caliente). El client OAuth del
addin (no secreto, sale del `.xll` público) en `settings.byma_client_*`. El secreto JWT y
la contraseña del admin: sección anterior y `docs/decisiones.md` D3.
