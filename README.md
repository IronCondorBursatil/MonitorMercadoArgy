# Monitor de Renta Fija AR

Monitor en tiempo real de renta fija argentina (soberanos hard-dollar, CER, tasa
fija, TAMAR/Dual, dólar-linked, Bopreales, futuros DLR, BEI, panel líder, FCI) +
cartera + ABM de catálogo. FastAPI + HTMX (SSR), pricing puro en `core/domain`,
datos de Data912 / BYMA / BCRA / CAFCI / dolarapi.

> **Arquitectura y convenciones**: ver [CLAUDE.md](CLAUDE.md) (guía del codebase,
> actual) y [agents.md](agents.md) (convenciones financieras: CER NT8/2024, TAMAR,
> BEI, day-counts, MD BYMA).

## Quick start

Python 3.12 del sistema, **sin venv en el proyecto** (el venv del servidor lo crea
`deploy.sh` y está gitignoreado). Las `.db` viven en `%LOCALAPPDATA%\monitor`
(**fuera del working tree de git**: la `catalog.db` es la fuente de verdad).

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"   # o: py -3.12

& $py -m pip install -r requirements.lock   # instalación reproducible (versiones fijas)
& $py run.py                                 # → http://localhost:8000  (o run.bat)
```

`requirements.txt` deja las versiones abiertas (para actualizar); `requirements.lock`
fija el estado conocido-bueno (reproducibilidad en otra máquina / tras un reset).

## Desarrollo

```powershell
pwsh scripts/check.ps1            # GATE: ruff + pytest (corré esto antes de cerrar branch)
& $py -m pytest tests/ -q         # solo tests (~1270)
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

La `catalog.db` es la **fuente de verdad viva** (altas de la ABM): la evolución de
schema es **forward-only** (nunca se dropea; ver `catalog_repository.init_db`).

## Configuración

`config/settings.py` (pydantic-settings), override por env `MONITOR_*`. Las
credenciales BYMA realtime van en `.env` (gitignored). Variables útiles:

- `MONITOR_MARKET_SOURCE` — `byma_open` (default) | `byma_realtime` | `data912`.
- `MONITOR_ENGINE_WORKERS` — workers del pricing (default `min(8, cpu_count)`).
- `MONITOR_TLS_NO_VERIFY_HOSTS` — hosts que saltean verificación TLS (default: los
  endpoints BYMA con cadena rota; el resto verifica). Ver `core/infrastructure/_tls.py`.
- `MONITOR_DISABLE_LOOPS` — desactiva los loops de fondo (lo usan los tests).
