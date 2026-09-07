---
paths:
  - "deploy/**"
  - "scripts/**"
  - "deploy.sh"
  - ".github/workflows/**"
---

# Deploy y scripts — reglas que cargan al tocar `deploy/`, `scripts/`, `deploy.sh`, workflows

Detalle en `docs/despliegue.md`; runbooks de la caja en `deploy/README-ops.md`. Tocar
`deploy.sh`, `gate.yml`, `check.ps1`, `requirements.txt` o el lock exige **autorización
explícita** de David (agents.md §0.1.6). Prod se toca SOLO por `deploy.sh`.

## `deploy.sh` es el único camino a producción

- Orden: `git pull origin main` → venv **3.12** validado (aborta con otra minor) → freeze
  ANTES → `pip install -r requirements.txt` (`--upgrade` = dentro de cotas, D2) → freeze
  DESPUÉS → restart → healthcheck `/api/health`. `set -euo pipefail`: si falla antes del
  restart, el servicio viejo sigue arriba.
- Freezes en `${MONITOR_DB_DIR:-/var/lib/monitor}/freeze/`. **Nunca `$MONITOR_DB_DIR`
  pelado**: con `set -u` aborta, porque el drop-in de systemd NO lo hereda una shell manual
  ni `ssh host 'cmd'`. Todo script a mano en prod lleva `MONITOR_DB_DIR=...` explícito.
- Instala `requirements.txt` (abierto), no el lock: el lock es de la laptop; el CI instala
  lo mismo que prod para cazar drift antes (`agents.md §0.3`). No apuntar `deploy.sh` al
  lock; no poner `==` en el `.txt`.
- No corre tests ni migraciones ni instala config del sistema (`deploy/bin/install-config.sh`
  con root, a mano). Los tests de `deploy/` (`test_ops_deploy_config.py`,
  `test_rem_R5_ops_tests_deploy_venv.py`, `test_aud_F_ops_deploy_lock.py`) pinean frases y
  flags: si uno falla por un hecho que cambió, corregir el archivo, no el test.
- Skills: `/deploy` (solo el usuario; `main` limpio → CI verde → ssh → salud → diff de
  freezes) y `/deps-refresh` (baja el freeze validado por el CI y regenera el lock con
  `scripts/relock.py`).

## Scripts que escriben el catálogo

- **`scripts/ingest_master.py`** (re-seed desde el Excel) tiene guards anti-pérdida: aborta
  con el server vivo (`op_guards.server_running`), si borraría altas DB-only, o si el backup
  pre-op falló; `--force` es override consciente y el snapshot pre-op es incondicional.
- **`core/infrastructure/on_catalog.ingest()`** es DESTRUCTIVO y SIN guards propios: borra la
  hoja `Obligaciones_Negociables` entera y la reconstruye del CSV; se lleva las ON que viven
  solo en la DB. Su único caller con guard es `app.py::_ensure_obligaciones_negociables`.
  Nunca desde un script sobre una DB poblada: para aplicar el CSV, ABM o append/upsert
  (`scripts/load_bond.py`).
- Un script nuevo que escriba la DB pasa por `scripts/op_guards.py` (`guard_write`,
  `guard_write_snapshot`), imprime contra QUÉ base corre y es dry-run por default
  (`backfill_tamar_anchor.py`, `migrate_orphan_types.py`, `sync_letras.py` son el modelo).
- `scripts/build_on_static.py` es la única forma de producir `apps/web/static/js/on.js`.

## Entorno y CI

- Intérprete `py -3.12` en todo header/comentario; nada de "Store Python" ni `WindowsApps`
  (`tests/test_aud_F_ops_docs_entorno.py` lo pinea). `check.ps1`/`check.sh` nombran el trunk
  `main` y el CI (`gate.yml`, x86 + `ubuntu-24.04-arm`, corre `scripts/check.sh`).
- `.github/workflows/`: `gate.yml` (cada push), `deps-refresh.yml` (semanal),
  `staleness.yml` (GET `/api/health` cada hora; no crear endpoints de salud nuevos). Los
  `schedule` corren con demora y se desactivan tras 60 días sin actividad.
- Pre-push: `scripts/install-hooks.ps1` (finales LF; `exec pwsh ... check.ps1 -Fast < /dev/null`).
- Skips de tests se asertan por motivo por plataforma (`tests/_skip_guard.py`), no por cuenta.

## TLS, timezone, secretos

- TLS se verifica siempre (`core/infrastructure/_tls.py`, allowlist vacía). Perilla:
  `MONITOR_TLS_NO_VERIFY_HOSTS`; nunca `verify=False`, `curl -k` ni `-SkipCertificateCheck`
  (están en `deny`).
- `apply_timezone()` fija ART en el proceso (prod corre en UTC); **no-op en Windows a
  propósito** (el CRT no parsea IANA). `last_refresh` es ART naive: medir con `age_seconds`.
- `.env` y `jwt_secret` no se leen ni se copian a ningún lado (`deny`). El primer admin:
  `scripts/init_admin.py` + `MONITOR_ADMIN_PASSWORD`, receta única en `docs/despliegue.md`.
