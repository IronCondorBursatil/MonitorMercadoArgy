# Monitor de Renta Fija AR

Monitor en tiempo real de renta fija argentina (soberanos hard-dollar, CER, tasa
fija, TAMAR/Dual, dólar-linked, Bopreales, obligaciones negociables, provinciales,
valor relativo, futuros DLR, BEI, panel líder, FCI, opciones, catálogo BYMA) +
cartera + ABM de catálogo. FastAPI + HTMX (SSR), pricing puro en `core/domain`.
Precios de **BYMA open** por default (conmutable en caliente a BYMA realtime o
Data912, con Data912 siempre de piso), más BCRA / CAFCI / ArgentinaDatos /
dolarapi / Matba-Rofex / FIX SCR.

> **Arquitectura y convenciones**: ver [CLAUDE.md](CLAUDE.md) (guía del codebase,
> actual) y [agents.md](agents.md) (convenciones financieras: CER NT8/2024, TAMAR,
> BEI, day-counts, MD BYMA).

## Quick start

Python 3.12 del sistema, **sin venv en el proyecto** (el venv del servidor lo crea
`deploy.sh` y está gitignoreado). Las `.db` viven en `%LOCALAPPDATA%\monitor`
(**fuera del working tree de git**: la `catalog.db` es la fuente de verdad).

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"   # o: py -3.12

& $py -m pip install -r requirements.lock -r requirements-dev.txt   # runtime + gate
$env:MONITOR_ADMIN_PASSWORD='...'; & $py scripts/init_admin.py      # SOLO la 1ª vez
& $py run.py                                 # → http://localhost:8000  (o run.bat)
```

**La app entera está detrás de login** y nadie crea el usuario por vos: en una máquina
(o droplet) nueva, sin correr `scripts/init_admin.py` antes, `/login` responde "Usuario o
contraseña incorrectos" para siempre y no hay forma de entrar. Si ya tenías la
`catalog.db` de antes —vive fuera del árbol, así que sobrevive a un clone nuevo— el admin
ya está ahí y este paso se saltea.

`requirements.txt` deja las versiones abiertas (para actualizar); `requirements.lock`
fija el estado conocido-bueno (reproducibilidad en otra máquina / tras un reset).

## Desarrollo

```powershell
pwsh scripts/check.ps1            # GATE: ruff + pytest (corré esto antes de pushear a main)
& $py -m pytest tests/ -q         # solo tests (~2330)
& $py -m ruff check .             # solo lint
```

El gate (`scripts/check.ps1`) es el equivalente a un CI corrido a mano: falla si ruff
o pytest fallan. Instalable como hook `pre-push`.

**Fecha de referencia de los tests**: los tests sensibles a la fecha (equivalencia,
golden) usan una fecha fija (`tests/_clock.py`, default 2026-06-10) para no caducar.
Para correr con la fecha real: `MONITOR_TEST_REF_DATE=today py -3.12 -m pytest`.
El "hoy" del **dominio** (ventana TAMAR, extrapolación CER, síntesis de cashflows)
se congela con `MONITOR_AS_OF=YYYY-MM-DD` (`core/domain/clock.py`) — **solo para
tests**: si queda activo, el server lo grita en WARNING al arrancar (los precios
usarían esa fecha, no la real).

## Operación

| Necesidad | Comando / mecanismo |
|-----------|---------------------|
| Backup del catálogo | Automático al arrancar (1×/día, rota a `MONITOR_BACKUP_KEEP=7` días) en `%LOCALAPPDATA%\monitor\backups`. |
| Restaurar un backup | `py -3.12 scripts/restore_catalog.py` (lista) · `--latest` · `<archivo>`. **Pará el server primero** (el script lo verifica y aborta; `--force` para saltear). |
| Estado / frescura | `GET /api/health` (`status` ok/degraded + `is_stale`/`last_error`) y el badge del header (verde/ámbar/rojo). |
| Re-sembrar catálogo del Excel | `py -3.12 scripts/ingest_master.py` (solo si editaste el master a mano) |
| Crear el primer usuario | `MONITOR_ADMIN_PASSWORD=... py -3.12 scripts/init_admin.py` — no hay bootstrap automático (ni el server ni `deploy.sh` lo hacen). |
| HTTPS en el droplet | `bash deploy/setup-https.sh <dominio> <email>` (como root, en el droplet; **requiere** un dominio con A a la IP — Let's Encrypt no emite para IP desnuda). Recién después: `MONITOR_COOKIE_SECURE=true` (uvicorn 0.48 ya trae `proxy_headers=True` y `forwarded_allow_ips="127.0.0.1"` por default — no hay que pasarle flags). |

La `catalog.db` es la **fuente de verdad viva** (altas de la ABM): la evolución de
schema es **forward-only** (nunca se dropea; ver `catalog_repository.init_db`).

## Configuración

`config/settings.py` (pydantic-settings), override por env `MONITOR_*`. Las
credenciales BYMA realtime van en `.env` (gitignored). Variables útiles:

- `MONITOR_MARKET_SOURCE` — `byma_open` (default) | `byma_realtime` | `data912`.
  `byma_realtime` además pide `BYMADATA_USER`/`BYMADATA_PASS` (se cargan desde la UI).
- `MONITOR_DB_DIR` — directorio de TODAS las bases y stores, **fuera del working tree**
  (default: `%LOCALAPPDATA%\monitor` en Windows, `~/.local/share/monitor` en Linux). Con
  esta sola variable alcanza: `catalog.db`, `backups/`, `history/` y los 4 stores cuelgan
  de ahí. Si alguna base resuelve adentro del repo, la app lo denuncia con un ERROR al
  arrancar (`MONITOR_DB_IN_TREE_FATAL=1` para que además aborte).
- `MONITOR_JWT_SECRET_KEY` — firma de la cookie de sesión. Sin ella se genera y persiste
  en `<db_dir>/jwt_secret` (0600). **Setearla en prod.**
- `MONITOR_COOKIE_SECURE` — flag `Secure` de la cookie del JWT. Default `false` **a
  propósito**: sobre HTTP el browser descartaría la cookie y el login queda en loop. Se
  activa recién cuando hay HTTPS (ver la tabla de Operación).
- `MONITOR_TRUSTED_PROXY_IPS` — peers cuyo `X-Forwarded-For` se cree para el rate-limit
  del login (default `127.0.0.1,::1`; vacío = no confiar en ningún XFF). Si el server
  queda detrás de un proxy distinto de nginx local, hay que declararlo acá o el limiter
  agrupa a todo el mundo en un bucket único.
- `MONITOR_TLS_NO_VERIFY_HOSTS` — hosts que saltean verificación TLS. Default **vacío**:
  desde 2026-09 se verifica TODO (los hosts BYMA que antes tenían la cadena rota se
  re-verificaron en vivo y encadenan bien). Ver `core/infrastructure/_tls.py`.
- `MONITOR_DISABLE_LOOPS` — desactiva los loops de fondo (lo usan los tests).
