# CLAUDE.md — Guía del codebase

Monitor de renta fija argentina (Soberanos, CER, Tasa Fija, TAMAR/Dual, Dólar Linked,
Bopreales, ONs, Provinciales, Valor Relativo, Futuros DLR, BEI, Panel Líder) + FCI +
Opciones + Catálogo BYMA + Cartera + ABM. FastAPI + HTMX SSR (`apps/web/`), motor de
pricing puro (`core/domain/`), ingesta async (`core/infrastructure/`), SQLite fuera del árbol.

**Precios: BYMA open** (`settings.market_source="byma_open"`; alternativas `byma_realtime` y
`data912`, conmutables en caliente desde `/source/*`) **con floor Data912 mergeado DEBAJO**:
cada ciclo el hub rellena con un snapshot Data912 los símbolos que la activa no lista y pisa
un precio 0 con un cierre real, porque un 0 no es dato (`provider_hub._apply_floor`). La
quote de una fila puede venir de `byma/field_map.byma_row_to_quote` **o** del floor, por
símbolo y por ciclo: si un precio sale raro, mirar las dos. Índices BCRA, futuros
Matba/Rofex, FX dolarapi, FCI CAFCI + ArgentinaDatos, calificaciones FIX SCR.

> **Antes de proponer una herramienta, tocar dependencias, configurar el agente
> (permisos/hooks/skills/rules) o ejecutar el plan de fases: leer `agents.md › §0 PROTOCOLO IA`**
> (versiones reales laptop/prod/CI, veredictos de herramientas, mecánica verificada de Claude
> Code, hechos del repo, plan por fases, glosario). Orden de autoridad: este archivo › §0 ›
> resto de `agents.md`. Las convenciones financieras (CER NT8/2024, TAMAR, BEI, day-counts,
> MD BYMA, float-only y tolerancias) viven en `docs/convenciones-financieras.md`.

## Cómo correr

```powershell
# Python 3.12 del sistema: `py -3.12` = %LOCALAPPDATA%\Programs\Python\Python312. Sin venv en el proyecto.
py -3.12 -m pip install -r requirements.lock -r requirements-dev.txt   # laptop: lock + dev
$env:MONITOR_ADMIN_PASSWORD='...'; py -3.12 scripts/init_admin.py     # SOLO la 1ª vez (docs/despliegue.md)
py -3.12 run.py                          # uvicorn → http://localhost:8000
py -3.12 -m pytest tests/ -q             # suite completa (fecha fija: tests/_clock.py)
pwsh scripts/check.ps1                   # GATE local: ruff + pytest (-Fast = -x)
pwsh scripts/install-hooks.ps1           # pre-push que corre el gate (una vez por clon)
py -3.12 scripts/ingest_master.py        # Excel → SQLite (solo si editaste el master a mano)
```

Skills del proyecto (`.claude/skills/`): `/gate` (ruff + pytest y cómo leerlo), `/smoke`
(app en :8001 + `/api/health` + una ruta), `/verificar-ui` (Playwright logueado),
`/compound` (cierre de fase), `/deploy` y `/deps-refresh` (solo los dispara el usuario).
CI: `.github/workflows/gate.yml` corre `scripts/check.sh` en x86 **y** ARM (~2,7 min) en cada
push; `deps-refresh.yml` semanal; `staleness.yml` vigila `/api/health` de prod cada hora.
Las `.db` viven en `settings.db_dir` (`%LOCALAPPDATA%\monitor`), fuera del árbol de git.
Qué instala cada entorno (lock en la laptop, `requirements.txt` en prod y CI): `agents.md §0.3`.

## Invariantes (no romper)

Cada uno es la regla + su porqué. La historia y el detalle están en `docs/` (índice abajo).

**Datos / catálogo**
- **SQLite (`catalog.db`) = fuente de verdad; Excel/CSV = semillas de bootstrap.**
  `CatalogRepository` auto-siembra del Excel solo si la DB está vacía; la ABM escribe SQLite
  directo y sus altas viven SOLO ahí (visibles en caliente vía `reload()`, que NUNCA
  re-siembra). Para cambiar datos ya en la DB: ABM o migración explícita, no re-seed.
  `scripts/ingest_master.py` tiene guards anti-pérdida (server vivo, altas DB-only, backup
  pre-op; `--force` es override consciente).
  `byma_catalog` sigue el mismo modelo: el CSV `data/byma/titulos_final.csv` se siembra
  SOLO si la tabla está vacía (`app._seed_byma_universe`, en el lifespan antes de los
  loops); después la mantiene el job de novedades.
- **`on_catalog.ingest()` es DESTRUCTIVO**: borra la hoja `Obligaciones_Negociables` entera y
  la reconstruye del CSV, sin snapshot ni guard de server vivo. El guard "solo si la hoja está
  vacía" vive en su único caller (`apps/web/app.py::_ensure_obligaciones_negociables`). Nunca invocarla
  a mano sobre una DB poblada: se lleva las ON que existen solo en la DB. Para aplicar el CSV:
  ABM o append/upsert (`scripts/load_bond.py`).
- **`universe.ingest_byma_catalog()` es DESTRUCTIVA** (DELETE+INSERT del CSV) y desde el
  job de novedades la tabla tiene estado propio (`last_seen`, símbolos del feed, ficha):
  se rechaza con filas `last_seen` salvo `force=True`. No hay script: es una operación de
  REPL (`py -3.12 -c "from core.infrastructure.byma.universe import ingest_byma_catalog;
  ingest_byma_catalog(force=True)"`) con el server parado y backup previo.
- **Alta automática de letras = la ÚNICA escritura automática en `instruments`**
  (`apps/web/letras_service.py` + `core/infrastructure/letras_sync.py`, al final de
  `_price_history_loop`, DESPUÉS del backup); el job diario de novedades del universo
  (`_universe_loop`, 08:00 AR) escribe SOLO `byma_catalog` y `universe_novedades`, nunca
  `instruments` (spec `docs/superpowers/specs/2026-09-07-novedades-universo-design.md`).
  Reglas duras, fijadas por tests: **sólo agrega**
  (sin update ni delete; las diferencias se reportan por WARNING), **sólo con dato completo**
  (sin `fechaEmision` no hay alta), **nunca una vencida**, **`tem: 0` es dato AUSENTE**, y
  descarta el payload ENTERO si trae <60 % de las letras vivas que ya hay. Escribe por
  `instruments_abm.save_instrument` (el borde con los guards), no por SQL. Perilla
  `MONITOR_LETRAS_AUTOSYNC=false`; a mano `scripts/sync_letras.py` (dry-run, `--apply`).
- **Payoff analítico ⇒ fila ANCLA, nunca schedule**: los tipos de
  `instrument_groups.ANALYTIC_PAYOFF_TYPES` (TAMAR PURO / DUAL / DUAL_CER_TAMAR) cobran por
  fórmula cerrada; materializarles un schedule es un error de datos. Llevan UNA fila
  `es_ancla=1` que `_orm_to_domain` filtra (el motor los ve con `cashflows=()`). Vale en las
  dos puertas de escritura (`save_instrument`/`save_cashflows` rechazan flujos) y en las tres
  de lectura (motor, form del ABM, preview). Backfill: `scripts/backfill_tamar_anchor.py`.
- **La ABM no sintetiza al guardar**: `cashflow_synth` lee el reloj (el step-up del cupón
  dependía del DÍA del alta). La síntesis es PREVIEW (`POST /abm/preview_cashflows`) que el
  operador revisa y el `<form>` manda de vuelta; un tipo normal sin flujos se **rechaza**.
- **Schema del catálogo = FORWARD-ONLY**: `init_db` agrega columnas (`ALTER ADD COLUMN`) y
  nunca dropea (un drop borra las altas ABM). Transformar datos = migración versionada
  (`CURRENT_SCHEMA_VERSION` + `schema_meta`), jamás recrear.
- **Nada de `.db` dentro del proyecto**: la base viva no va en el árbol donde corre
  `git pull`/`git clean`. `Settings._check_db_paths` lo denuncia por ERROR; un store nuevo se
  cuelga de `_DB_DERIVED` para que `MONITOR_DB_DIR` lo reubique con el resto.
- **Un `instrument_type` fuera de `core/domain/instrument_groups.py` deja el bono INVISIBLE**: el
  read-path filtra por igualdad exacta; la fila se carga y acumula precio pero no se precia ni
  se muestra. `core/infrastructure/repositories.py::_resolve_instrument_type` es el ÚNICO que decide el tipo (avisa por WARNING si
  lo asume o queda huérfano); `save_instrument` rechaza con `is_known_type()`; el estado vivo
  sale en el bloque `catalog` de `/api/health`. Nunca inventar el tipo del nombre de la hoja.

**Pricing**
- **Equivalencia del motor**: `tests/test_pricing_equivalence.py` compara contra el motor
  congelado `tests/_legacy_engine.py` (tolerancia 1e-7) sobre todos los instrumentos. Todo
  cambio de pricing la deja verde. El legacy comparte solver/metrics/30-360 a propósito: no
  copiarle símbolos.
- **`FinancialEngine` preserva firmas** públicas (consumidores: `bond_detail`, `generate_report`).
- **V.Téc / payoff DUAL_CER_TAMAR**: settlement T+N → lag CER 10 hábiles → spread → max de
  rieles, en ese orden (ya se rompió dos veces). Contrato paso a paso en el docstring de
  `core/domain/pricing/tamar.py`: leerlo ANTES de tocar `tamar_dual_payoff_at` o
  `calculate_technical_value`.
- **Un `0`/`≤0` de una fuente externa es dato AUSENTE**, no un valor (precio 0 de la activa,
  `ccp<=0` en FCI, `tem: 0` en letras): se descarta, no se usa.

**Web**
- **`apps/web/static/js/on.js` es AUTO-GENERADO** por `scripts/build_on_static.py` desde
  `apps/web/on_src/`. No editarlo (deny de permisos + hook lo bloquean): el gate pasa igual y
  la próxima regeneración borra el fix. Tocar `apps/web/on_src/` y regenerar.
- **Rutas públicas = `tests/test_aud_G_tests_route_auth.py::_PUBLIC_PATHS`** (`/login`,
  `/logout`, `/api/health`, `/static`). Todo router nuevo lleva `RequireTabPermission`; no
  crear endpoints de salud nuevos; `/api/health` es público y por eso recortado.

**Infra / robustez**
- **Timeouts de httpx**: el centinela es `httpx.USE_CLIENT_DEFAULT`, **NO `None`** (`None` =
  sin timeout: un request colgado cuelga el loop para siempre). Copiar
  `async_http._USE_CLIENT_DEFAULT` en cualquier wrapper. Guarda: `tests/test_aud_A_infra_http_timeouts.py`.
- **TLS se verifica siempre** (`core/infrastructure/_tls.py`), allowlist VACÍA. Si una cadena
  se rompe, la perilla es el env `MONITOR_TLS_NO_VERIFY_HOSTS`; nunca `verify=False` en un
  cliente (deja el override inerte).
- **Intérprete `py -3.12`** (Programs; el Store Python ya no existe). Sin venv en el proyecto.

**Método**
- **Este archivo gana sobre las skills** y sobre el resto de `agents.md`. TDD aplica a
  features, bugfixes y refactors; un test que no se pone rojo al revertir el fix es decorativo
  (probar la mutación). No debilitar tests, tipos ni validaciones para que algo pase.

## Convenciones al CREAR archivos nuevos

Las rules por path no se disparan con `Write` de un archivo nuevo; por eso viven acá.
- **Tipo de instrumento nuevo**: primero `core/domain/instrument_groups.py`, después usarlo.
- **Provider nuevo**: async por `ResilientClient` (`core/infrastructure/async_http.py`) con el
  centinela de timeout; el camino sync (CAFCI/argentinadatos) no tiene retry y no crece.
- **Store SQLite nuevo**: colgarlo de `_DB_DERIVED` en `config/settings.py`.
- **Test nuevo de pricing/golden**: fecha fija con `tests/_clock.py` (no `date.today()`);
  override `MONITOR_TEST_REF_DATE`. Golden externo = con procedencia (fuente, fecha, captura).
- **Test nuevo de web**: `TestClient`; corre como admin por la fixture autouse `_auth_bypass`;
  auth real = `@pytest.mark.noauth`; recorrer rutas con `tests/_routes.py`, nunca `app.routes`.
- **Router nuevo**: `RequireTabPermission("<tab>")` en `apps/web/app.py`; `/security-review` antes de pushear.
- **Script nuevo en `scripts/`**: `py -3.12` en el header; en prod corre con `MONITOR_DB_DIR`
  explícito; si escribe la DB, pasa por `scripts/op_guards.py` y es dry-run por default.
- **Panel nuevo**: registrarlo en `PANELS`/`PANEL_ORDER` (`apps/web/routers/panels_schema.py`);
  si no filtra por tipos (estilo futuros/VR/BEI) además su builder en `panels_rows._build_rows`.
  Receta completa: `.claude/rules/web.md › Receta: panel nuevo`.

## Cuándo leer qué

| Si vas a… | Leé |
|---|---|
| Proponer herramientas, tocar deps, configurar el agente, ejecutar el plan | `agents.md › §0` (0.1 reglas, 0.3 versiones, 0.4 veredictos, 0.5 Claude Code, 0.8 fases) |
| Tocar pricing (`core/domain/pricing/**`, `core/domain/*.py`) | `.claude/rules/pricing.md` (carga sola al leer) + `docs/convenciones-financieras.md` |
| Tocar `apps/web/**` | `.claude/rules/web.md` (sola) + `docs/flujo-web.md` |
| Tocar login / permisos / usuarios | `.claude/rules/auth.md` (sola) + `docs/auth.md` |
| Tocar `deploy/`, `scripts/`, `deploy.sh`, workflows | `.claude/rules/deploy.md` (sola) + `docs/despliegue.md` |
| Entender el mapa completo (qué vive dónde, supervisor, observabilidad) | `docs/arquitectura.md` |
| Deployar, configurar prod, backups, TLS, timezone, primer admin | `docs/despliegue.md` → runbooks en `deploy/README-ops.md` |
| Saber qué está pendiente / decidido / medido | `docs/pendiente.md` · `docs/decisiones.md` · `docs/baseline-2026-09.md` |
| Permisos, hooks y skills del proyecto | `.claude/README.md` |
| Cerrar una fase / feature / bugfix | `/compound` (agents.md §0.1.9) |

## Flujo de trabajo (Superpowers)

Plugin **Superpowers 6.3.0**: `brainstorming → spec (delta) → writing-plans → TDD/subagentes →
code-review → finishing-branch`; artefactos en `docs/superpowers/` (`specs/`, `plans/`). Las
skills se cargan al arrancar Claude Code, no en caliente. **Prioridad: este CLAUDE.md > skills**
(si una skill choca con una convención de acá, manda acá). **Worktrees sí, nunca dentro del
proyecto** (`EnterWorktree` o `~/.config/superpowers/worktrees/`). **Los planes escriben
`py -3.12`**, nunca `python`/`pytest` pelados. Una mejora por vez, gate verde antes de pushear.
