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
# SIEMPRE con el Microsoft Store Python 3.12 (no el de Programs ni `py -3.12`):
$py = "$env:LOCALAPPDATA\Microsoft\WindowsApps\python3.12.exe"
& $py run.py                          # levanta uvicorn → http://localhost:8000
& $py -m pytest tests/ -q             # tests (≈131)
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
core/infrastructure/
  db/        engine.py (SQLite+WAL) · models.py (ORM 2.0) · catalog_repository.py (CatalogRepository, drop-in del ExcelRepo)
  analytics/duck.py    cer_asof / avg_tamar (DuckDB sobre los CSV de history)
  async_http.py circuit_breaker.py provider_hub.py   primitivas async (httpx + breaker + hub) — testeadas
  schemas.py           Data912Row (validación Pydantic en el borde de ingesta)
  _http.py             http_get_json SYNC (httpx pooled) — usado por los providers (sync, vía to_thread)
  repositories.py indices_provider.py fx_provider.py futures_provider.py rem_provider.py cafci_provider.py
apps/web/
  app.py               FastAPI + lifespan (refresh loop + BEI loop vía asyncio.to_thread). MONITOR_DISABLE_LOOPS en tests.
  state.py deps.py      AppState (snapshot vivo) + Depends (get_repo→CatalogRepository, get_state, get_provider, ...)
  routers/             panels (12 paneles + /bond detalle), bonds, cartera, bcra, cashflows, abm
  templates/           base.html + pages/* + fragments/* (Jinja + HTMX)
  static/css/app.css   diseño Balanz (light/dark) · static/vendor/gridstack
  bond_detail.py instruments_abm.py cartera_store.py   (reusados por los routers)
run.py scripts/ tests/ data/ config/
```

## Flujo web (HTMX SSR)

`run.py`→uvicorn→`app.py`. El **lifespan** arranca 2 tasks: `_refresh_loop` (cada 5s,
corre `GenerateMonitorReport.execute` vía `to_thread` → `AppState`) y `_bei_loop`
(`compute_bei_tables`). Cada panel es un `<tbody hx-get="/panels/{id}/rows" hx-trigger="every 5s">`
que renderiza un fragmento server-side desde `AppState`. El detalle es un modal
(`/bond/{t}/detail` + `/bond/{t}/metrics` calculadora).

## Invariantes (no romper)

- **Equivalencia del motor**: `tests/test_pricing_equivalence.py` compara el motor nuevo
  contra el original congelado (`tests/_legacy_engine.py`) sobre todos los instrumentos.
  Cualquier cambio de pricing debe dejarlo verde.
- **`FinancialEngine` preserva firmas** públicas (sus consumidores: bond_detail, generate_report).
- **Excel = semilla**: `CatalogRepository` lee SQLite (auto-siembra del Excel si vacío);
  la ABM edita el Excel y llama `reload()` para re-sembrar. `ingest_master.py` re-siembra.
- **Store Python**: ver `~/.claude/.../memory/env_python_interpreter.md`. `py -3.12` es el INTÉRPRETE EQUIVOCADO.
- **OneDrive**: nada de venv ni `.db` dentro del proyecto.

## Pendiente (cola, no funcional)
Reescritura async de los 6 providers (cablear `ProviderHub`/`ResilientClient`, retirar `_http.py` sync);
charts/sparklines (Chart.js island); ABM transaccional SQLAlchemy (§5.5, hoy escribe Excel+reseed);
valor_relativo de soberanos (universo curado). Ver `mejora.md` + memoria `reingenieria-progress`.
