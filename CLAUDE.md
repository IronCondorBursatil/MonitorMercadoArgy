# CLAUDE.md — Guía del codebase (post-reingeniería)

Monitor de renta fija argentina (Soberanos, CER, Tasa Fija, TAMAR/Dual, Dólar Linked,
Bopreales, Futuros DLR, BEI, Panel Líder) + Cartera + ABM. Precios de **Data912**,
índices de **BCRA**, futuros de **Matba/Rofex WS**, FX de **dolarapi**.

> Este archivo describe la arquitectura **actual** (tras implementar `mejora.md`).
> `agents.md` conserva las **convenciones financieras** (CER NT8/2024, TAMAR, BEI,
> day-counts, MD BYMA) que siguen 100% vigentes — pero su descripción de la capa
> **web** es vieja (era http.server + SPA `app.js`). La verdad actual es la de acá.

## Cómo correr

```powershell
# Python 3.12 del sistema (el de Programs; `py -3.12` resuelve a él). El antiguo
# "Microsoft Store Python" ya NO existe en esta máquina — ver memoria env_python_interpreter.
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"   # o simplemente: py -3.12
& $py -m pip install -r requirements.lock   # instalación reproducible (versiones fijas)
& $py run.py                          # levanta uvicorn → http://localhost:8000
& $py -m pytest tests/ -q             # tests (~1270)
pwsh scripts/check.ps1                # GATE: ruff + pytest (antes de cerrar branch)
& $py scripts/ingest_master.py        # Excel → SQLite (cuando editás el master a mano)
```

`run.py` arranca **uvicorn** sobre `apps/web/app.py:app` (FastAPI). Config en
`config/settings.py` (`settings`, pydantic-settings; override por env `MONITOR_*`).
Las `.db` viven en `%LOCALAPPDATA%\monitor` (fuera de OneDrive).

## Arquitectura

```
core/domain/
  models.py            Pydantic v2: Cashflow/Instrument (frozen) + MarketSnapshot. InstrumentMetrics=dataclass.
  services.py          FinancialEngine — FACHADA delgada (preserva firmas) que delega al pricing core.
  xirr.py conventions.py   funciones puras (XIRR act/365.25, 30/360, tasas, settlement, tamar_tem, cer_ref).
  pricing/
    protocols.py context.py   IndicesProvider/FxProvider (Protocol) + PricingContext (inmutable).
    base.py            VanillaStrategy: camino general (vanilla + CER inline + 30/360 inline + LECAP).
    strategies.py      Cer / DolarLinked / Tamar / DualCerTamar (overrides + super() fallback).
    registry.py        strategy_for(inst) — tabla predicado→strategy (mata la escalera if/elif).
    metrics.py tamar.py stubs.py   métricas popup / payoff BONTE TAMAR / ZeroTamar.
  cashflow_synth.py portfolio.py scenarios.py yield_curve.py inflation_path.py   (sin cambios)
  fci/         dataset del panel FCI (puro, testeable): derive.py (subcategoría estilo fonditos + AUM join ArgentinaDatos + unificación de clases x fondo) · hist.py (cuotaparte reconstruida de retornos reales / real de fci_history) · lens.py (devaluación A3500 + inflación CER por período) · dataset.py (build_fci_dataset → forma que consume static/js/fci.js). Composición NO se sintetiza; flujos solo reales.
core/infrastructure/
  db/        engine.py (SQLite+WAL, reconfigurable p/ tests) · models.py (ORM 2.0, + sheet/raw_fields del ABM) · catalog_repository.py (CatalogRepository, drop-in del ExcelRepo; reseed_with_meta)
  async_http.py circuit_breaker.py provider_hub.py   ingesta async (httpx + breaker + hub). ProviderHub.refresh_all + HubMarketDataProvider CABLEADOS al refresh loop.
  schemas.py           Data912Row (validación Pydantic en el borde de ingesta)
  _http.py             http_get_json SYNC (httpx pooled) — read-path de los 5 providers sync (FX/indices/REM/CAFCI/argentinadatos + histórico), corren en to_thread fuera del event loop
  repositories.py indices_provider.py fx_provider.py futures_provider.py rem_provider.py cafci_provider.py
  price_history.py     store SQLite auto-mantenido de cierres diarios (rendimientos Sem/1M/3M/YTD/1A): priming Data912 /historical/bonds + acumulación del feed vivo; read-path local (merge con el CSV legacy)
  fci_history.py       store SQLite (fuera de OneDrive) de vcp/ccp/patrimonio por fondo (ArgentinaDatos), acumulado a diario por el loop → flujo neto real Δccp×VCP (`net_flow_series`). cafci_provider._parse_payload conserva los campos ricos de CAFCI (honorarios/horizonte/duration/region/tickers/min/objetivo)
  repositories.build_instrument()   parser de fila → Instrument, COMPARTIDO por el loader Excel y el ABM SQLite
apps/web/
  app.py               FastAPI + lifespan (refresh loop con hub.refresh_all async + BEI loop, motor vía to_thread). MONITOR_DISABLE_LOOPS en tests.
  state.py deps.py      AppState (snapshot vivo + revision/wait_for_change p/ SSE) + Depends (get_repo→CatalogRepository, get_state, get_hub, ...)
  routers/             panels (12 paneles + /bond detalle), bonds, cartera, bcra, cashflows, escenarios, curva, fci (página + /fci/data JSON), abm, stream (SSE)
  fci_service.py       junta CAFCI enriquecido + AUM + macro (lens A3500/CER) + flujos (fci_history) → dataset memoizado de /fci/data
  templates/           base.html + pages/* + fragments/* (Jinja + HTMX)
  static/css/app.css   diseño Balanz (light/dark) · static/css/fci.css · static/vendor/gridstack · static/js/fci.js (app cliente del panel FCI: 5 vistas + detalle, Chart.js)
  bond_detail.py instruments_abm.py cartera_store.py   (reusados por los routers)
run.py scripts/ tests/ data/ config/
```

## Flujo web (HTMX SSR)

`run.py`→uvicorn→`app.py`. El **lifespan** arranca las tasks: `_refresh_loop` (cada 5s:
`await hub.refresh_all()` trae Data912 async con breaker+pool, luego el motor corre
`GenerateMonitorReport.execute` vía `to_thread` leyendo el snapshot del hub → `AppState`),
`_bei_loop` (`compute_bei_tables`) y `_price_history_loop` (mantiene el store de cierres
diarios para los rendimientos: priming Data912 + acumulación del feed; **además acumula el corte
diario de ArgentinaDatos en `fci_history` para los flujos reales del panel FCI**). Cada panel es un `<tbody hx-get="/panels/{id}/rows">`
que renderiza un fragmento SSR desde `AppState`; el auto-refresh es **event-driven por SSE**
(`/stream` pushea `refresh` por ciclo; `hx-trigger="load, sse:refresh, every 15s"` con el
polling como fallback). El detalle es un modal (`/bond/{t}/detail` + `/bond/{t}/metrics`).

**Panel FCI** (excepción al SSR): `GET /fci` sirve una página que carga `static/js/fci.js`
(app cliente vanilla, 5 vistas + detalle con Chart.js), que hace `fetch('/fci/data')`. El dataset
lo arma `fci_service.get_fci_dataset` (memoizado por corte/día, servido con GZip) combinando CAFCI
enriquecido + AUM ArgentinaDatos + lente A3500/CER + flujos reales de `fci_history`.

## Invariantes (no romper)

- **Equivalencia del motor**: `tests/test_pricing_equivalence.py` compara el motor nuevo
  contra el original congelado (`tests/_legacy_engine.py`) sobre todos los instrumentos.
  Cualquier cambio de pricing debe dejarlo verde.
- **`FinancialEngine` preserva firmas** públicas (sus consumidores: bond_detail, generate_report).
- **SQLite (`catalog.db`) = fuente de verdad; Excel/CSV = semillas de bootstrap** (decisión v7.2):
  el catálogo VIVO es SQLite. `CatalogRepository` lee SQLite (auto-siembra del Excel si vacío vía
  `ingest_master.py` / `ingest_from_excel`, que preserva `sheet`+`raw_fields`). Las **ONs** siembran
  de `data/obligaciones_negociables.csv` vía `on_catalog.ingest()` (bootstrap **solo si la hoja está
  vacía** — no re-ingesta destructiva). La **ABM escribe SQLite directo** (SQLAlchemy transaccional,
  §5.5 — ya NO toca el Excel) y es el editor de runtime; sus altas viven SOLO en la DB y se ven
  EN CALIENTE (save → `reload()` del repo singleton → el ciclo siguiente del motor las precia;
  sin reiniciar — test `test_abm_save_alta_visible_sin_reiniciar`). `reload()` refresca el cache
  en memoria desde SQLite (NUNCA re-siembra; el camino destructivo vive solo en
  `ingest_from_excel`/`ingest_master.py`). Para cambiar datos ya en la DB: ABM o migración
  explícita, no re-seed. El re-seed (`ingest_master.py`) tiene guards anti-pérdida: aborta con el
  server vivo, si borraría altas DB-only, o si el backup de seguridad pre-reseed falló
  (`--force` para override consciente; el snapshot pre-op es incondicional, `backup_db(tag=...)`).
- **Intérprete Python**: usar `py -3.12` / `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`. Ver memoria `env_python_interpreter`. (El viejo "Store Python" ya no existe; sus deps se reinstalaron acá.)
- **OneDrive**: nada de venv ni `.db` dentro del proyecto.
- **Schema del catálogo = FORWARD-ONLY**: `init_db` reconcilia con el ORM agregando
  columnas faltantes (`ALTER ADD COLUMN`), **nunca** dropea — un drop borraría las altas
  ABM (que viven solo en la DB). Para transformar datos existentes: migración versionada
  (`CURRENT_SCHEMA_VERSION` + `schema_meta`), jamás recrear. Ver `db/catalog_repository.py`.

## Robustez / Operaciones

- **Backup del catálogo**: snapshot online (consistente con WAL) 1×/día al arrancar,
  rota a `settings.backup_keep` (7) en `%LOCALAPPDATA%\monitor\backups`. Restore:
  `scripts/restore_catalog.py`. Ver `core/infrastructure/db/backup.py`.
- **Verificación TLS por host** (`core/infrastructure/_tls.py`): se **verifica por
  defecto** (anti-MITM); solo saltea los hosts con cadena rota (endpoints BYMA
  addin/open — verificado en vivo). Override `MONITOR_TLS_NO_VERIFY_HOSTS`. NO volver a
  poner `verify=False` global.
- **Observabilidad**: si el refresh loop falla, `AppState.record_error` lo registra y el
  header lo muestra (badge `/health/badge` verde/ámbar/rojo) + `/api/health` da
  `status`/`is_stale`/`last_error`. La app sigue sirviendo el último snapshot bueno.
- **Gate de calidad**: `scripts/check.ps1` (ruff + pytest). Correrlo antes de cerrar
  branch. `requirements.lock` = instalación reproducible.
- **Fecha fija en tests** (`tests/_clock.py`): los tests de equivalencia/golden usan
  una fecha de referencia fija (no `date.today()`) para no caducar. Override
  `MONITOR_TEST_REF_DATE=YYYY-MM-DD|today`.
- **Secretos**: credenciales BYMA del usuario en `.env` (gitignored). El client OAuth
  del addin (no secreto, sale del `.xll` público) en `settings.byma_client_*`.

## Flujo Superpowers (método de trabajo)

Este repo usa el plugin **Superpowers** (obra). El flujo para features nuevas es:
`brainstorming → spec → writing-plans → plan → TDD/subagent-driven → code-review →
finishing-branch`. Artefactos en `docs/superpowers/` (`specs/`, `plans/` — ver su
README). **Las skills se auto-disparan al arrancar Claude Code** (no en caliente).

- **Prioridad**: las instrucciones de este CLAUDE.md **ganan** sobre las skills. Si una
  skill choca con una convención de acá (financieras de `agents.md`, equivalencia del
  motor, Excel=semilla), manda CLAUDE.md.
- **TDD aplica** a features nuevas, bugfixes y refactors (test rojo → mínimo verde →
  refactor). Encaja con la disciplina ya existente: `test_pricing_equivalence.py` y los
  137 tests son la red. Excepción: prototipos descartables / config (consultar antes).
- **Worktrees + OneDrive**: worktrees **sí**, pero **nunca** `.worktrees/` dentro del
  proyecto (OneDrive + regla de [[feedback_no_venv]]). Usar la tool nativa
  `EnterWorktree` del harness, o el path global `~/.config/superpowers/worktrees/`
  (fuera de OneDrive). Subagentes paralelos: ojo con la sincronización de OneDrive si se
  trabaja in-place.
- **Intérprete en los planes**: los comandos de test/run deben usar `py -3.12` (ver
  invariante de abajo), no `python`/`pytest` pelado.

## Pendiente (cola, no funcional)

- **Providers sync restantes**: FX/indices/REM/CAFCI/argentinadatos siguen sync vía `_http.py`.
  Es **deliberado**: se llaman dentro del cómputo de pricing (que corre en `to_thread`, fuera del
  event loop) y tienen cache propio con TTL; el hot-path real (Data912, 4 endpoints cada 5s) ya
  está en el hub async. Migrarlos a `ResilientClient` (y retirar `_http.py`) es bajo valor / alto
  riesgo — queda como cola opcional.
- Charts/sparklines adicionales (Chart.js) — ya usado en el panel FCI (`static/js/fci.js`); extender a otros paneles. Más cobertura de tests de routers.
- **FCI composición de cartera**: única pieza no disponible (CAFCI ficha gateada / worker de fonditos pago). El panel la omite hasta conseguir fuente. Flujos: reales vía `fci_history` a medida que acumula ruedas; lente 3m/6m/12m se completa cuando el bootstrap de ~400d de CER/A3500 backfillee.
Ver `mejora.md`.
