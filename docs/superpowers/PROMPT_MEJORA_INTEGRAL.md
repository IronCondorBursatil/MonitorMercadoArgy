# PROMPT MAESTRO — Campaña de mejora integral del "Monitor de renta fija argentina"

## 0. ROL Y OBJETIVO

Sos un **ingeniero de plataforma senior** (Python 3.12 / FastAPI / SQLite / HTMX) operando como agente de coding sobre el repositorio **`Monitor de renta fija argentina`** (monitor de Soberanos, CER, Tasa Fija, TAMAR/Dual, Dólar Linked, Bopreales, Futuros DLR, BEI, Panel Líder, ONs + Cartera + ABM + FCI). Precios de Data912/BYMA, índices de BCRA, futuros de Matba/Rofex, FX de dolarapi.

Tu **meta** es conducir una campaña por *epics* que mejore, en este orden de prioridad, **(1) correctitud financiera y de datos, (2) robustez/operaciones, (3) performance/agilidad, (4) calidad/simplificación, (5) frontend/UX, (6) cobertura de tests, (7) deuda técnica** — **sin romper jamás los invariantes de la Sección 2**.

**Cómo se mide el éxito** (no negociable, en cada PR):
- `tests/test_pricing_equivalence.py` **verde** (motor nuevo == `tests/_legacy_engine.py` congelado, tol 1e-7, todo el master a 95/130/158.2).
- `tests/test_balanz_golden.py` **verde** (anclas de mercado Balanz, tol 1.5bp en TIR; incluye `test_cica_is_756_not_757_regression`).
- `pwsh scripts/check.ps1` **verde** (ruff `select=F,E,W` + `pytest tests/ -q`, hoy ~1370 tests).
- **Cero regresión visible** de paneles (los 12+ fragmentos SSR y las apps cliente `/on`, `/fci` siguen renderizando con el mismo shape de datos).
- Cada mejora viene con su **test rojo→verde** (TDD) y un criterio de aceptación medible.

No escribís features especulativas: trabajás el backlog de la Sección 5, priorizado.

---

## 1. MAPA DEL PROYECTO

Arquitectura en 3 capas (web → use_cases → domain ← infrastructure):

- **`core/domain/`** — Pydantic v2 frozen (`models.py`: `Cashflow`/`Instrument`, `MarketSnapshot` mutable a propósito). Motor de pricing reingenierizado: `services.py::FinancialEngine` es una **fachada delgada** (~242 líneas, preserva firmas) que delega a `pricing/registry.py::strategy_for(inst)` (tabla predicado→strategy, `registry.py:26-40`). `pricing/base.py::VanillaStrategy` = camino general (soberanos + LECAP/BONCAP + CER inline + 30/360 inline). `pricing/strategies.py` = 5 familias (Cer, DolarLinked, HardDollar, Tamar, DualCerTamar) que override + `super()`. `pricing/metrics.py` = métricas por instrumento (accrued/residual/dv01/convexity/stub ISMA). `pricing/context.py::PricingContext` inmutable. Funciones puras: `xirr.py` (Newton+Brent, overflow-safe), `daycount.py` (year_fraction 4 convenciones), `conventions.py` (tamar_tem k=365/32, cer_reference_date, settlement BYMA), `cashflow_synth.py` (days_30_360 ISDA), `clock.py` (`today()` con override `MONITOR_AS_OF`). FCI puro en `core/domain/fci/`.
- **`core/infrastructure/`** — `db/` (engine SQLite+WAL reconfigurable, ORM 2.0, `catalog_repository.py::CatalogRepository` drop-in del ExcelRepo, `backup.py`), ingesta async (`provider_hub.py::ProviderHub` + `HubMarketDataProvider`, `async_http.py::ResilientClient` con CircuitBreaker, `circuit_breaker.py`, `schemas.py::Data912Row`, `byma/field_map.py` + `byma/universe.py`), providers sync vía `_http.py` (indices BCRA, fx dolarapi, rem, cafci, argentinadatos), stores locales `price_history.py` (cierres+volumen) y `fci_history.py` (vcp/ccp/patrimonio). `on_classification.py::sector_for` (clasificación ON, única fuente).
- **`core/use_cases/generate_report.py`** — `GenerateMonitorReport.execute` corre `_enrich_metrics` por instrumento en `ThreadPoolExecutor`. **OJO**: la dolarización de la pata ARS soberana vive acá (`_sovereign_ars_usd_price`), **NO en el motor**, deliberadamente para no romper la equivalencia (agents.md:306).
- **`apps/web/`** — `app.py` (FastAPI + lifespan con 4 tasks: `_refresh_loop` 5s, `_bei_loop` 300s, `_price_history_loop` 3600s, `_startup_reconcile`). `state.py::AppState` (snapshot vivo + revision + wait_for_change p/SSE). Routers en `routers/` (panels, bonds, cartera, bcra, cashflows, escenarios, curva, fci, abm, stream/SSE, on, options, source, header, catalog). `panels.py` (863 líneas, router-god). `panels_schema.py` (schema declarativo, data pura). `instruments_abm.py` (810), `bond_detail.py` (963), `on_service.py`, `fci_service.py`. Templates Jinja (autoescape ON) + fragments HTMX. Apps cliente JS vanilla: `static/js/fci.js`, `static/js/on.js` (**auto-generado** por `scripts/build_on_static.py` — NO editar a mano). **Libs JS vendoreadas LOCALES en `static/vendor/`** (htmx, htmx-ext-sse, chart.umd, html2canvas, gridstack); `base.html:11-14`/`index.html:5` las sirven desde `/static/vendor/`, NUNCA desde CDN.
- **Flujo web**: `run.py`→uvicorn→`app.py`. El refresh loop (5s) hace `await hub.refresh_all()` → motor en `to_thread` → `AppState.update` → SSE `refresh` → cada `<tbody hx-get=/panels/{id}/rows>` recarga (`hx-trigger="load, sse:refresh, every 15s"`). FCI y ON son apps cliente que hacen `fetch('/fci/data')` / `fetch('/on/data')`.
- **ABM** escribe SQLite directo (transaccional), altas visibles en caliente vía `reload()` del repo singleton (que **nunca re-siembra**).

---

## 2. INVARIANTES NO NEGOCIABLES (GUARDRAILS)

> **Si una mejora choca con cualquiera de estos, MANDA EL INVARIANTE. Abandonás la mejora o la rediseñás para respetarlo. No hay excepciones.**

1. **EQUIVALENCIA DEL MOTOR** — `tests/test_pricing_equivalence.py` vs `tests/_legacy_engine.py` (congelado, ~46KB) debe quedar **verde**. Ningún refactor de `core/domain/pricing/*`, `services.py`, `xirr.py`, `daycount.py`, `metrics.py` puede mover TIR/MD/V.Téc/paridad/precio de **ningún** instrumento. La fixture `_frozen_clock_and_clean_caches` (congela ambos motores a `ref_date()` vía `MONITOR_AS_OF` + `_FrozenDate`) es intocable salvo para extender cobertura.
2. **GOLDEN DE BALANZ** — `tests/test_balanz_golden.py` (14+ anclas reales, tol 1.5bp). Cualquier cambio de day-count/accrued/stub que mueva CICA/CACB/BPCV/CLISA/YM42/etc. está prohibido. La **red real** contra errores de day-count son estos golden (la equivalencia comparte `pricing.metrics` y es "verde por construcción" en day-count).
3. **`FinancialEngine` preserva firmas públicas** — sus consumidores (`generate_report`, `bond_detail`, bei, options) llaman `calculate_tir`/`technical_value`/`duration` con los mismos kwargs `settle_date`/`settle_lag`. Cambiar la firma de un `@staticmethod` rompe esos call-sites.
4. **Convenciones financieras de `agents.md` INTACTAS**: CER NT8/2024 (`cer_base` = CER 10 días hábiles antes de emisión, factor `cer_val/cer_base` 1× en V.Téc), TAMAR (`tamar_tem` k=365/32, payoff `100×(1+TEM_max)^N`, MD bullet m=12, `project_cer_at` COMPUESTO), BEI, day-count **por instrumento** vía `inst.year_fraction_to()` (NUNCA recablear a 365.25 fijo — ese era el bug v7.2), **ex-cupón corte ESTRICTO** (futuros `> ref`, pasados `<= ref`, en lockstep con el legacy), stub ISMA (3 condiciones calibradas CLISA-extiende/YM42-no), settlement T+1 en el loop / T+0|T+1 en la calculadora.
5. **La dolarización de la pata ARS soberana vive en `generate_report._enrich_metrics`/`_sovereign_ars_usd_price`, NO en el motor.** Moverla adentro rompe la equivalencia. MEP para BONAR/BOPREAL (ley local), CABLE para GLOBAL (ley NY).
6. **SQLite (`catalog.db`) = fuente de verdad viva; Excel/CSV = semilla de bootstrap.** `reload()` **NUNCA** re-siembra (`catalog_repository.py:301-306`). El camino destructivo (wipe+seed) vive solo en `ingest_from_excel`/`ingest_master.py` con guards anti-pérdida. `on_catalog.ingest()` solo bootstrapea si la hoja está vacía.
7. **Schema del catálogo FORWARD-ONLY** — `init_db`/`_migrate_table_add_columns` solo `ALTER ADD COLUMN` (o índice nuevo), **JAMÁS drop/recreate** (borraría altas ABM DB-only). Transformaciones de datos → migración versionada (`CURRENT_SCHEMA_VERSION` + `schema_meta`).
8. **Thread-safety del hub**: el snapshot lo MUTA el event loop y lo LEE el threadpool del motor — mantené `threading.Lock` y la copia bajo lock (`provider_hub.py:159-167,270-274`). No introducir estado mutable de módulo con lock como el que la reingeniería eliminó. Strategies **stateless** (singletons en el registry). `PricingContext` frozen.
9. **Stale-safe del hub**: `_merge` NUNCA wipea ante un ciclo vacío. Precedencia activa>floor: la fuente activa manda donde lista; el floor Data912 SOLO aporta símbolos no cubiertos y solo si `row.c>0`. Validación en el borde (`parse_snapshot_rows`/`byma_row_to_quote`) descarta **fila por fila**, no tumba el batch. Fallback `LAST→closingPrice→previousClosingPrice` intacto.
10. **TLS verify por DEFECTO** (anti-MITM). La allowlist no-verify es mínima (2 hosts BYMA con cadena rota, `_tls.py:25`), configurable por env. **NUNCA `verify=False` global** ni `follow_redirects` cross-host con el cliente no-verify.
11. **Clock inyectable**: la red sensible a fecha usa `tests/_clock.ref_date()` (DEFAULT 2026-06-10) + `MONITOR_AS_OF`. **Todo test nuevo date-sensitive DEBE anclar a ese clock, NUNCA a `date.today()` crudo.** No reintroducir `date.today()` hardcodeado en el pricing core.
12. **Entorno físico**: nada de venv ni `.db` dentro del proyecto (OneDrive corrompe SQLite mid-write). `.db` + backups en `%LOCALAPPDATA%\monitor`. Intérprete = `py -3.12` / `%LOCALAPPDATA%\Programs\Python\Python312\python.exe` (NO el viejo "Store Python"). Worktrees **nunca** dentro del proyecto (usar `EnterWorktree` o `~/.config/superpowers/worktrees/`).
13. **`MONITOR_DISABLE_LOOPS=1` en tests** apaga los loops (contaminan caches de módulo como el avg TAMAR). No tocar el `setdefault` de `conftest.py` ni quitar el disable.
14. **Shape de datasets servidos**: `/on/data` (`generated, today, bonds[], sectors[], meta{}`, con escalas tir/parity ×100, change_pct YA en %), `/fci/data` (`{meta, funds}`), `panel_rows.html` (`rows`/`cells`/`columns`) — los consumen `on.js`/`fci.js`/Jinja. Refactors de servicio NO cambian las claves.
15. **`on.js` es AUTO-GENERADO** por `scripts/build_on_static.py` (concatena `docs/mockups/on/_shared/{sectors,util,unified}.js` + mock 21 con `_require()/replace` de anclas `THEME_OLD`/`INIT_OLD`). Cualquier cambio al cliente `/on` se hace en las fuentes y se **regenera**; editar `on.js` directo se pierde.
16. **`AppState.record_error` NO dispara SSE** (`state.py:49-54`): registra el fallo como par `(msg[:300], ts)` con **asignación atómica** y sigue sirviendo el último snapshot bueno. Despertar a los paneles ante un error era un request-storm en outages (~12/min × panel); el header se entera por su propio polling (`/health/badge`). **NUNCA** notificar SSE desde el path de error.
17. **`AppState` = un solo escritor por campo + publicación atómica** (`state.py:114-136`): `update`/`set_bei`/`set_data_source`/`set_options` cada uno con su único escritor (el loop que corresponde). En `set_options` se **construye el índice ANTES de publicar las referencias** para que los lectores del threadpool nunca vean un dict a medio llenar. Cualquier campo nuevo en `AppState` (p.ej. el CI de C4) DEBE seguir este patrón: un solo escritor (el `_refresh_loop`), índice antes de lista, asignación de la referencia completa.
18. **`CircuitOpenError` / breaker abierto NO tumba el ciclo**: el caller (`provider_hub`/`sources`) lo captura y devuelve `{}` → el `_merge` stale-safe conserva el último snapshot bueno. Endurecer la validación de datos (A5) NO debe convertir un breaker abierto en una excepción que rompa `refresh_all`.
19. **`Instrument` frozen + cashflows cronológicos**: el `field_validator _sort_cashflows` (`models.py:77-84`) ordena el schedule al construir; **`model_construct` no se usa en el repo** (sin bypass del validator). Toda alta/edición ABM y todo synth DEBE pasar por el constructor normal (jamás `model_construct`), o el pricing leería flujos desordenados y daría resultados silenciosamente erróneos.

---

## 3. ENTORNO Y COMANDOS

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"   # o: py -3.12
& $py -m pip install -r requirements.lock   # instalación reproducible
& $py run.py                                 # uvicorn → http://localhost:8000 (NO dejar colgado)
& $py -m pytest tests/ -q                    # suite (~1370)
& $py -m pytest tests/test_pricing_equivalence.py tests/test_balanz_golden.py -q   # red financiera
pwsh scripts/check.ps1                        # GATE canónico = ruff + pytest (antes de cerrar branch)
pwsh scripts/check.ps1 -Fast                  # = pytest -x (corta en el 1er fallo)
& $py scripts/ingest_master.py                # Excel → SQLite (re-seed, con guards; --force override)
& $py scripts/restore_catalog.py              # lista backups; <archivo>|--latest para restaurar
```

- El gate NO tiene CI remoto: es local/manual. **"Verde" = `scripts/check.ps1` en exit 0**, no solo el test que tocaste.
- Worktrees: usá la tool nativa `EnterWorktree` o el path global fuera de OneDrive; **nunca** `.worktrees/` dentro del proyecto.
- Los comandos de los planes deben usar `py -3.12`, no `python`/`pytest` pelado.

---

## 4. MÉTODO DE TRABAJO

Flujo **Superpowers**: `brainstorming → spec → writing-plans → plan → TDD/subagent-driven → code-review → finishing-branch`. Artefactos en `docs/superpowers/` (`specs/`, `plans/`).

- **TDD OBLIGATORIO** (rojo→mínimo verde→refactor) para features, bugfixes y refactors. La red existente (`test_pricing_equivalence`, golden, los ~1370) es tu paracaídas. Excepción: prototipos descartables/config (consultar antes).
- **Prioridad de instrucciones**: este prompt y `CLAUDE.md`/`agents.md` **ganan** sobre cualquier skill. Si una skill choca con un invariante, manda el invariante.
- **1 branch + 1 PR por epic** (ver Sección 9). No mezclar epics en un branch.
- **Mensajes de commit** en español, descriptivos, terminando con la línea Co-Authored-By correspondiente. **No commitear ni pushear sin pedido explícito.**
- **Correr el gate ANTES de cerrar** cada branch. Si toca pricing, correr además la red financiera completa.
- Si encontrás un riesgo no contemplado o una ambigüedad de invariante, **pará y preguntá** antes de tocar el motor o la capa de datos.

---

## 5. BACKLOG PRIORIZADO POR EPICS

> Cada tarea trae: **archivo:línea · enfoque · TEST a agregar · CRITERIO DE ACEPTACIÓN · RIESGO · ROLLBACK**. Los 15 hallazgos del code-review están incorporados en el epic que corresponde, correctitud primero.

### EPIC A — CORRECTITUD (máxima prioridad)

**Objetivo**: eliminar bugs que producen datos/comportamiento incorrectos o pérdida de datos, sin tocar el motor congelado. **Toda alta/synth respeta el invariante 19 (constructor normal, jamás `model_construct`; cashflows quedan cronológicos).**

- **A1 · El restore borra su propio target por rotación** — `scripts/restore_catalog.py:74-84` crea el snapshot `pre-restore` con `keep=settings.backup_keep`, lo que dispara la rotación en `core/infrastructure/db/backup.py:86` (`list_backups()[:-keep]`) **antes** de que `restore_db` lea el archivo en la línea 84. Si ya hay `keep` backups y el target es el más viejo (`backups[0]`), la rotación lo borra → `restore_db` aborta con `FileNotFoundError`.
  - *Enfoque*: en `restore_catalog.py:74` hacer el snapshot pre-restore con `keep=0` (en `backup.py:86`, `keep>0` gatea la rotación → con 0 no rota) y dejar que el daily del próximo arranque pode; o pasar a `backup_db` un `protect: set[Path]` excluido del `[:-keep]`. Lo más simple y seguro: `keep=0` para el tagged.
  - *TEST*: `test_restore_catalog.py` — directorio con `keep` backups lleno, restaurar `backups[0]` (el más viejo) → debe completar y el target NO debe haber sido borrado antes del read.
  - *Aceptación*: restaurar cualquier backup explícito (no solo `--latest`) completa siempre. La red de seguridad pre-restore se sigue creando incondicional.
  - *Riesgo*: bajo. *Rollback*: revertir el `keep`.

- **A2 · Pool de rotación único daily+tagged ("keep ARCHIVOS", no "keep días")** — `backup.py:32` globa `catalog-*.db` sin distinguir prefijo; la rotación recorta a `keep=7` los más recientes por nombre. Una tarde de re-seeds/altas de ON genera 7+ tagged (`pre-reseed`/`pre-restore`/`pre-irsa-ons`/`pre_on_emisor_refresh`) que rotan-out TODOS los daily previos.
  - *Enfoque*: opción A (mínima) — rotar daily (`catalog-YYYY-MM-DD.db` sin `T`) y tagged por separado, cada uno a su `keep`. Opción B (correcta) — rotar por antigüedad real en DÍAS parseando la fecha del nombre. Alinear docstrings (`backup.py:8`, `settings.py:67`, `CLAUDE.md:110`) que prometen "keep días".
  - *TEST*: `test_db_backup.py` — N daily + M tagged en un día → tras rotar, los daily previos sobreviven; `test_backup_rotation_keeps_n_most_recent` sigue verde; el `T`+hora sigue ordenando tagged después del daily.
  - *Aceptación*: una jornada con varias operaciones destructivas no evapora el historial diario.
  - *Riesgo*: bajo. *Rollback*: revertir la separación de pools.

- **A3 · El floor Data912 nunca rellena un símbolo que la activa dejó de listar / cayó a 0** — `provider_hub.py:112` acumula `_active_syms` para SIEMPRE (solo se vacía en `set_source`); `_apply_floor` (`:147`) descarta del floor todo símbolo en `_active_syms`. Si BYMA listó un símbolo un día y al siguiente NO (cambio de panel, pre-market, parcial), el floor ya no lo cubre → el motor lo precia con un precio rancio sin marca de antigüedad. El símbolo que cae a 0 (filtrado por `c>0`) tampoco se reescribe.
  - *Enfoque*: reemplazar el `set` acumulado por el conjunto del ciclo actual unido a una **ventana de K ciclos** (anti-flicker). **Fijar K = 3 ciclos** (≈15s) como default configurable; un símbolo sale de `_active_syms` recién tras K ciclos consecutivos sin que la activa lo liste, y ahí el floor Data912 lo recupera. La activa SIEMPRE manda donde lista este ciclo.
  - *TEST*: `test_source_switch.py` (clock fijo, multi-ciclo determinístico) — activa lista `{A,B}` ciclos 1..3, luego `{A}` en ciclos 4..6 → `B` sigue cubierto por la activa hasta el ciclo 3 post-desaparición (K=3) y desde ahí lo cubre el floor; verificar que NO hay oscilación A↔floor ciclo a ciclo dentro de la ventana K.
  - *Aceptación*: en un proceso de varios días el floor no se "envenena"; un símbolo que la activa deja de listar vuelve a tener cierre Data912 tras K ciclos, sin parpadeo.
  - *Riesgo*: medio (oscilación). *Rollback*: revertir a `set` acumulado.

- **A4 · Marca de frescura por símbolo + purga conservadora de stale viejo** — `provider_hub.py:159-167` (`_merge` solo `update`) y `:270-274` (`snapshot()` devuelve todo): un delistado o un precio caído a 0 sigue eternamente con el valor viejo, sin distinción "fresco hoy" vs "visto hace 3 días".
  - *Enfoque*: `_seen_at[sym]` paralelo actualizado en `_merge`/`_apply_floor`; `snapshot()` mantiene su firma (back-compat), agregar accessor `freshness()`; purga 1×/día en `_price_history_loop` o al rollover de `date.today()`, solo para filas claramente muertas (> N días sin tocar), nunca un blip transitorio. Mantener thread-safety del lock (invariante 8) y stale-safe (invariante 9): un ciclo vacío NO purga nada.
  - *TEST*: símbolo no refrescado > umbral → purgado en rollover; un ciclo vacío NO purga nada (stale-safe).
  - *Aceptación*: el panel puede distinguir vivo de rancio; el motor no precia precios de hace días como si fueran live.
  - *Riesgo*: medio. *Rollback*: la purga es opcional y aditiva — desactivar el umbral.

- **A5 · `Data912Row` acepta NaN/Inf como precio "válido"** — `schemas.py:15-37`: `c: float = Field(ge=0)` deja pasar `float('nan')`/`inf` (NaN compara False en toda relación, no es `< 0`). Ese NaN/Inf fluye al XIRR/MD y al store de cierres. Sin sanity `bid<=ask`.
  - *Enfoque*: `field_validator` que use `math.isfinite()` sobre `c` (rechaza NaN/Inf), normalice `px_bid`/`px_ask`/`v` no finitos a `None`. Replicar el guard de finitud en `byma/field_map.py:75`. Un raise en el validator = "fila descartada" fila-por-fila (invariante 9), **no tumba el batch ni convierte un breaker abierto en excepción** (invariante 18).
  - *TEST*: `test_byma_field_mapping.py` — fila con `c=NaN`/`Inf` descartada; el fallback `LAST→close→prev_close` de especies ilíquidas sigue funcionando; un breaker abierto sigue devolviendo `{}` sin propagar.
  - *Aceptación*: ningún valor no finito llega al motor ni al store.
  - *Riesgo*: bajo. *Rollback*: quitar el validator.

- **A6 · `_safe_synth` (rama tolerante del form ABM) sin test del contrato + ruta AttributeError→500** — `instruments_abm.py:92-105` atrapa SOLO `ValueError/KeyError/TypeError` → `[]`; `AttributeError` propaga a propósito (bug interno no se traga → no guarda alta con cero cashflows en silencio). Sin test, ampliar el except a `Exception` pasa el gate.
  - *Enfoque*: pinnear el contrato del `except` acotado. Sobre el hallazgo "AttributeError→500": rastrear el call-site del router que invoca el form/synth y decidir explícitamente — si un input legítimo dispara `AttributeError` y hoy devuelve un 500 opaco, distinguir **bug interno** (debe seguir propagando/500, NO tragarse) de **input inválido** (debe ser un mensaje de validación visible). NO ampliar el except para "arreglar" el 500.
  - *TEST*: `test_instruments_abm.py` — `_safe_synth({...basura...}) == []`; con `synth_cashflows` monkeypatcheado lanzando `AttributeError`, `pytest.raises(AttributeError)`. Test de router para el input que hoy da 500 (afirmando el comportamiento deseado, no tragándolo).
  - *Aceptación*: el except queda acotado; un bug real dentro de synth no se traga; el alta nunca se guarda con cero cashflows en silencio.
  - *Riesgo*: bajo. *Rollback*: trivial (solo test).

- **A7 · Prefill HARD DOLLAR en el form ABM** *(hallazgo del review)* — `instruments_abm.py:307` (`"options": ["HARD DOLLAR", "DOLLAR LINKED"]`) y `:388` (`tipo = "HD" if "HARD DOLLAR" in itype else ...`).
  - *Enfoque*: **PRIMERO confirmar si hay bug con un caso concreto.** Tomar un ticker hard-dollar real del master (p.ej. un BONAR/GLOBAL o una ON …D ya cargada) como fixture-ancla, dar de alta vía el prefill ＋Alta del Universo BYMA y afirmar: (1) `instrument_type`/`tipo` resultante = el esperado (HD), (2) la strategy resuelta por `strategy_for` = `HardDollarStrategy` (no `DolarLinkedStrategy`), (3) la dolarización de la pata pesos = MEP (ley local) / CCL (extranjera), sin invertir. Si el prefill ya mapea bien, **cerrar la tarea como verificación sin cambio y documentarlo**; tocar `instruments_abm.py:307/388` solo si el fixture rojo lo demuestra.
  - *TEST*: `test_instruments_abm.py`/`test_abm_router.py` — alta con prefill HARD DOLLAR del ticker-ancla produce `type`+strategy+dolarización esperados; reconciliar con `bond_detail.calculate(..., settlement_lag=1)`.
  - *Aceptación*: el alta preciada coincide con la convención documentada; sin regresión de clasificación.
  - *Riesgo*: medio (toca clasificación). *Rollback*: revertir el mapeo.

- **A8 · Cupón corriente en bono amortizing — VERIFICACIÓN (no bug confirmado)** *(hallazgo del review)* — `on_service.py:55-71` (`_current_coupon`) computa `nxt.interest/dcf` sobre el flujo del schedule synth, que ya es el residual amortizado → **probablemente correcto**.
  - *Enfoque*: tarea de **verificación, no de fix automático**. Anclar un amortizing real del master (ticker concreto) con su current-yield esperado tomado de la calculadora Balanz. Si `_current_coupon` ya divide `nxt.interest/dcf` sobre el flujo residual del synth y coincide con el ancla → **NO hay bug: cerrar sin cambio y documentar el ticker/valor verificado.** Solo tocar si el ancla diverge. Preferir registrar amortizing por campos del form (synth, memoria `prefer-synth-over-explicit-cashflows`) sobre cashflows explícitos.
  - *TEST*: golden/router — current-yield del amortizing-ancla coincide (tol del golden) con la calculadora.
  - *Aceptación*: la columna Current Yield del panel ON reconcilia con el ancla Balanz; si ya reconcilia, no se mueve ningún valor.
  - *Riesgo*: medio (mover un valor que no estaba roto). *Rollback*: revertir.

- **A9 · TIR falsy-zero (`not m.tir` vs `m.tir is None`)** *(hallazgo del review)* — auditar dónde se filtra por TIR. Sitios YA correctos: `panels.py:115` y `:624` usan `not m.duration or m.duration <= 0 or m.tir is None`.
  - *Enfoque*: grep `not m\.(tir|duration|md)` en `panels.py`/`bond_detail.py`/`on_service.py`/`generate_report.py`. **CRÍTICO — NO TOCAR `panels.py:115` ni `:624`**: ahí `not m.duration or m.duration <= 0` es CORRECTO a propósito — una duration de 0.0 es input inválido para `math.log(m.duration)` en el fit de la curva (`tir_fitted = a + b*log(duration)`); "arreglarlo" a `is None` rompería el fit con `math.log(0)`. El fix se limita a sitios donde **0.0 es un valor legítimo a mostrar/incluir** (una TIR de 0.0 real que se descarte por `not m.tir`). Reemplazar `not m.tir` por `m.tir is None` solo ahí.
  - *TEST*: `test_panels_router.py` — instrumento con TIR=0.0 aparece en el panel donde corresponde; y `test_curva_router.py` confirma que el fit de curva sigue verde (duration 0 sigue excluida, sin `log(0)`).
  - *Aceptación*: ninguna TIR 0.0 legítima se descarta por falsy-check; el fit de curva intacto (los sitios `not m.duration` permanecen).
  - *Riesgo*: bajo. *Rollback*: revertir el condicional.

- **A10 · Guard de continuidad en `fci_history`** — `fci_history.py:91-99` indexa por nombre de fondo normalizado (`_norm`, lowercase/sin acentos); un renombre/colisión de ArgentinaDatos parte o mezcla la serie de `ccp` → un salto espurio se lee como flujo gigante en `net_flow_series` (`:144-156`).
  - *Enfoque*: en `net_flow_series` clampear/saltear el flujo cuando `|ccp[d]-ccp[prev]|/ccp[prev]` supera un umbral configurable (un fondo no dobla/colapsa su circulación en una rueda); emitir warning throttled al descartar. La función sigue pura (no toca disco). El primer punto nunca tiene flujo.
  - *TEST*: serie con salto implausible de `ccp` → el flujo de ese punto se clampea/descarta; saltos legítimos grandes no se borran silenciosamente.
  - *Aceptación*: un renombre no inventa una suscripción/rescate masivo en el panel FCI.
  - *Riesgo*: bajo. *Rollback*: quitar el clamp.

- **A11 · `record_live_closes` graba intradía como "cierre" — VERIFICACIÓN/cuantificación (NO schema-change automático)** — `price_history.py:295-316`. El propio docstring (`price_history.py:303-304`) afirma que **"hoy nunca es base de una ventana (impacto chico)"**, lo que contradice la premisa de pérdida grave.
  - *Enfoque*: **BAJA prioridad. Primero cuantificar el impacto real**: confirmar contra el código si un reinicio intradía efectivamente sesga alguna ventana Sem/1M/3M/YTD/1A para tickers sin priming (bopreales/letras/ON/patas/CER nuevos), o si el comentario del código es correcto y el impacto es nulo/chico. **Solo si se demuestra sesgo real** evaluar el cambio aditivo forward-only (flag "intradía vs cierre confirmado" + consolidación EOD en `_price_history_loop`, o re-correr `prime_from_byma_historico` para pisar con el cierre T-1). El read-path (`get_series`) sigue 100% local sin red. Si el impacto es chico/nulo → documentar y cerrar sin tocar schema.
  - *TEST* (solo si se procede): cierre intradía marcado no se usa como base de ventana hasta confirmarse.
  - *Aceptación*: impacto cuantificado; si se toca schema, las variaciones no se sesgan por un reinicio intradía; si no, decisión documentada de no-cambio.
  - *Riesgo*: medio (schema aditivo) — evitable si la verificación cierra la tarea. *Rollback*: la columna queda (forward-only), desactivar el uso.

### EPIC B — ROBUSTEZ / OPERACIONES

**Objetivo**: endurecer backup/restore y observabilidad sin tocar pricing.

- **B1 · Limpiar sidecars WAL en el path de restore** — `backup.py:92-101` (`restore_db`) escribe sobre la `.db` viva sin tocar `dst-wal`/`dst-shm`; un crash previo deja sidecars huérfanos que sombrean el contenido restaurado. Como `restore_db` ya exige server apagado (guard del script), borrar `dst-wal`/`dst-shm` (`missing_ok=True`) antes del `_online_copy` es seguro. **No** cambiar el `journal_mode` del runtime (WAL por concurrencia, `db/engine.py:29`). *TEST*: dejar un `catalog.db-wal` stale, restaurar, abrir, verificar el conteo del backup. *Riesgo*: medio. *Rollback*: revertir el unlink.
- **B2 · Backup periódico desde el loop** — único disparo automático en `app.py:310-317` (arranque). Agregar a `_price_history_loop` (`app.py:173-247`) una llamada best-effort a `backup_db` 1×/día calendario (idempotente por día, `backup.py:72-74`); captura el estado aunque el server lleve días levantado. Envolver en try (nunca rompe el loop). *Riesgo*: bajo.
- **B3 · Alinear doc "keep días" vs "keep archivos"** — si no se adopta rotación por días (A2-B), ajustar docstrings (`backup.py:8`, `settings.py:67`, `CLAUDE.md:110`). Solo texto. *Riesgo*: nulo.
- **B4 · Caches sync indexados a `date.today()` sin TZ de mercado** — `indices_provider.py:118-124,156-158`, `cafci_provider.py:177-181`, `rem_provider.py:77-80`: el gate "1 intento/día" usa hora local; Reservas BCRA se publican ~18-20hs AR. Documentar/ajustar el gate para no saltar el dato del día si el server cruza medianoche AR. **CRITERIO VERIFICABLE — el negative-cache y el offline-fallback NO se debilitan**: el cooldown anti-storm (`rem` `FAIL_COOLDOWN`, `cafci` `_RETRY_COOLDOWN_S`) sigue activo y testeado, y un fetch fallido sigue devolviendo el último valor cacheado (offline-friendly) sin re-stormear. *TEST*: el gate respeta el día de mercado AR; un fallo sigue gateado por el cooldown; el fallback offline devuelve el cache previo. *Riesgo*: bajo.

### EPIC C — PERFORMANCE / AGILIDAD

**Objetivo**: bajar el costo de los hot-paths sin perder frescura ni determinismo bajo pytest.

- **C1 · `count_unloaded()` materializa todo el universo BYMA en cada carga de `/abm`** — `byma/universe.py:303-307` llama `search_byma_grouped("","",limit=10_000_000)` (full scan ~6.4k filas + grouping en Python + `_ficha_isins()` + `_loaded_ids()`, dos full-scans de `instruments`) solo para devolver un `int`. *Enfoque*: helper de conteo en SQL (`EXISTS`/subquery) o memoizado por (revisión del catálogo, día) — solo cambia al dar de alta. *TEST*: el número "sin cargar" coincide con `g['loaded']` (`search_byma_grouped:240-247`). *Riesgo*: medio. *Rollback*: revertir al call original.
- **C2 · Índices SQLite en `instruments.ticker_mep`/`ticker_ccl` + WHERE indexado** — `db/models.py:61-62` sin `Index` (contraste con byma_catalog `:49-52`); `_find_bond_row`/`_find_bond_rows` (`instruments_abm.py:519,589`) y `_loaded_ids` (`universe.py:157-166`) caen a select-all+filtro Python. *Enfoque*: declarar `Index('ix_instr_mep',...)`/`ix_instr_ccl` (aditivo, forward-only, `create_all` los crea); reescribir los lookups con `where(or_(ticker==t, ticker_mep==t, ticker_ccl==t))`. *TEST*: la resolución por pata sigue encontrando la fila-bono en cualquier slot. *Riesgo*: bajo. *Rollback*: quitar índices (forward-only: quedan, no molestan).
- **C3 · `list_instruments_coverage` carga cashflows (`selectin`) que no usa** — `instruments_abm.py:433-436` + `db/models.py:82-87` (`lazy='selectin'`) trae TODOS los cashflows aunque el % de completitud venga de `cfcounts` (query aparte). *Enfoque*: `.options(lazyload(cashflows))`/`noload` en las queries del ABM que solo cuentan. *TEST*: el % no cambia; no se dispara el SELECT IN de cashflows. *Riesgo*: bajo.
- **C4 · `_ci_metrics` re-ejecuta el motor entero por request sin cache** — `panels.py:457-473,591-594`: con `settle=CI` construye `HubMarketDataProvider(settle=CI)` y corre `GenerateMonitorReport.execute` en el hilo del request (se dispara por SSE cada 5s si el panel quedó en CI). *Enfoque*: **opción preferida** memoizar por `(state.revision, 'CI')` (espejo de `on_service.py:156-167`) — un solo `execute()` por revisión, reusado entre requests. La **opción alternativa** "calcular CI dentro de `_refresh_loop` y guardarlo en `AppState`" SOLO es válida si: (a) respeta el invariante 17 (el `_refresh_loop` es el único escritor del campo CI nuevo, índice antes de lista, referencia completa), y (b) **NO duplica el costo del motor por ciclo** — hoy el loop corre `execute()` 1× (24hs); agregar CI sería 2× por ciclo cada 5s, lo que puede no compensar si pocos paneles usan CI. **Criterio de decisión medible**: contar `execute()` por ciclo y por request antes/después; elegir la opción que minimice cómputo total dado el uso real de CI (si CI es raro, el memo por revisión es mejor; si es constante, el loop). CI usa `settle_date=today`/`settle_lag=0`. *TEST*: dos requests CI con la misma revisión reusan un cómputo; al cambiar la revisión recalcula; si se elige el loop, un solo escritor publica la referencia completa. *Riesgo*: medio. *Rollback*: revertir el cache/campo.
- **C5 · Variaciones Sem/1M/3M/YTD/1A recalculadas cada 5s** — `generate_report.py:118,123-128`: por ciclo, N copias de dict (`get_series`, `price_history.py:94-103`) + 5N scans lineales `_asof_price`, aunque la base (cierre histórico) cambia 1×/día. *Enfoque*: memoizar por `(ticker, día)` las `px_7d/30d/90d/ytd/1y` (solo dependen del histórico + fecha objetivo); `calculate_pct_change(price, base)` es trivial y sigue por ciclo. El cache no debe re-introducir red ni devolver bases caducadas al cruzar medianoche; bajo pytest el read-path es determinístico (loops apagados → store vacío → CSV legacy). *TEST*: la base se recalcula al cruzar el día; mismas `tol_days` por ventana. *Riesgo*: medio. *Rollback*: revertir el cache.
- **C6 · `GenerateMonitorReport` instancia providers en `execute()`** — `generate_report.py:50-51` crea `BCRAIndicesProvider`/`DolarAPIProvider` adentro pese a que `app.py:321-322` ya tiene singletons; `_bei_loop`/`_ci_metrics` los recrean por loop/request. *Enfoque*: params opcionales en `__init__` (`indices=None`, `fx=None`) que defaulteen **exactamente** a `BCRAIndicesProvider(excel_repo=self.repo)` + `DolarAPIProvider()` (mismo objeto que hoy → ningún número cambia); los call-sites pasan los singletons de `app.state`. *TEST*: el use-case se testea con providers mock sin parchear; equivalencia intacta. *Riesgo*: bajo. *Rollback*: revertir el `__init__`.
- **C7 · (oportunidad, opcional) SSE selectivo / hash por panel** — hoy `/stream` pushea `refresh` por ciclo y los 12 `<tbody>` re-fetchean aunque su fragmento no haya cambiado. *Enfoque exploratorio*: hash del payload por panel en `AppState` y un evento SSE por-panel (o un `Last-Event-ID`/etag por fragmento) que dispare solo los `<tbody>` cuyo hash cambió. **Respetar invariantes 16 (nunca SSE en error) y 17 (single-writer del hash en el loop).** Es OPORTUNIDAD, no obligatoria; abordarla solo con la red verde y midiendo el ahorro real de requests/render. *Riesgo*: medio. *Rollback*: volver al `refresh` global.

### EPIC D — CALIDAD / SIMPLIFICACIÓN / ALTITUD

**Objetivo**: subir la altitud del código sin cambiar comportamiento (cero impacto en pricing/datos).

- **D1 · Extraer los builders de filas de `panels.py` (863 líneas, router-god)** — separar el router (index/panel_rows/chart/share/layout) de la lógica de construcción (`_build_rows`/`_build_rv_rows`/`_build_*_rows`/`_chart_payload`/`_share_*`/`_fit_log_curve`/`_rv_map`) a `apps/web/panels_rows.py` (espejo de `on_service`/`fci_service`). Actualizar los 2 imports externos (`on_service.py:14`, `curva.py:20`) para que no importen helpers privados de un router. **El shape de `panel_rows.html`/`panel_share.html` no cambia; los endpoints devuelven el mismo HTML.** *TEST*: `test_panels_router.py`/`test_curva_router.py` verdes (200s, mismo HTML). *Riesgo*: bajo.
- **D2 · Unificar "moneda por sufijo de ticker" (duplicada en 4-5 sitios)** — `portfolio.py:26` (`position_currency`, USD/ARS solo por D, gateado por `_USD_TYPES`), `panels.py:77` (`_ticker_ccy`, MEP/CABLE/ARS), `instruments_abm.py:62` (`_sob_slot`), `bond_detail.py:50` (`_is_usd_quoted`), `repositories.py:157-159`. *Enfoque*: un sub-helper canónico `_ccy_from_suffix(ticker) -> 'MEP'|'CABLE'|'ARS'` (en `portfolio.py` o `currency.py`); los 4 call-sites lo envuelven traduciendo a su shape (slot del form / USD-vs-ARS gateado por `_USD_TYPES`). **La unificación NO debe reasignar la moneda de ningún ticker** (D=MEP, C=CABLE, resto=ARS; `position_currency` sigue dando USD solo para `_USD_TYPES` con D). *TEST*: parametrizado de la convención fijando cada call-site **antes** de unificar. *Riesgo*: medio. *Rollback*: revertir el helper.
- **D3 · `on_service` reimplementa lógica del dominio** — `on_service.py:30-47` (`_peso_fx_rate`, clon de `strategies.py:103-161` HardDollar/DolarLinked) y `:55-71` (`_current_coupon`). *Enfoque*: crear `core/domain/pricing/fx_legs.py::peso_leg_to_usd(inst, fx)` puro que centralice MEP/CCL/ley/oficial; que `_sovereign_ars_usd_price` (enrich) y `on_service._peso_fx_rate` lo importen. **El motor (`strategies.py`) NO la usa** (mantiene su copia para no entrar al perímetro de equivalencia — agents.md:306, invariante 5). *TEST*: el panel ON dolariza igual que hoy. *Riesgo*: medio. *Rollback*: revertir a los clones.
- **D4 · `_TEMPLATES = Jinja2Templates(...)` duplicado en 14 routers** — crear `apps/web/templates.py` con `TEMPLATES` singleton (patrón de `apps/web/json_script.py`); cada router: `from apps.web.templates import TEMPLATES as _TEMPLATES`. El directorio resuelto debe ser idéntico (`parent.parent/'templates'`). *TEST*: `test_*_router.py` (200s). *Riesgo*: bajo.
- **D5 · Clasificar los 87 imports function-local** — subir a top-level los de conveniencia (la mayoría en `app.py`/routers que no rompen ciclo); dejar y anotar los que rompen ciclo real (`models.py:146/154`, `metrics.py:102/135`); cachear a nivel módulo el símbolo en hot-paths (`_ci_metrics`, `panels.py:461-463`). Smoke-test de import al final de cada lote para detectar ciclos. No alterar `MONITOR_DISABLE_LOOPS` ni el orden de import del lifespan. *Riesgo*: bajo. *Rollback*: bajar de nuevo el import.

### EPIC E — FRONTEND / UX

**Objetivo**: cerrar el XSS, matar duplicación de fuente, mejorar a11y — sin romper el shape de los datasets y **sin reintroducir dependencias remotas**.

- **E1 · XSS almacenado en `on.js` (ALTA)** — `on.js:371,1018,299,1011,1017` inyectan `e.name`/`bond.emisor`/`b.clase`/`sm.short` SIN escapar en `innerHTML`/`title=`; el origen es editable desde la ABM (`panels.py:297-298`: short_name→emisor, serie_clase→clase) y persiste en SQLite. Un valor con `"><script>` ejecuta JS. *Enfoque*: definir `esc()` (replace `&<>"'`) **en `docs/mockups/on/_shared/util.js`** (no en `on.js`, que es auto-generado) y envolver SOLO los campos de texto crudo (los numéricos ya pasan por `ON.num`/`ON.pct`); **regenerar `on.js` con `scripts/build_on_static.py`**. *TEST*: un emisor con markup se renderiza escapado (test de `on_service`/router o smoke del bundle). *Aceptación*: ningún string editable llega sin escapar a `innerHTML`/`title`. *Riesgo*: bajo. *Rollback*: revertir la fuente + rebuild.
- **E2 · XSS en `fci.js` (MEDIA)** — `fci.js:216-217,255,221` inyectan `f.fondo`/`f.soc` (de CAFCI, fuente externa ~3.9MB sin auth) sin escapar (28 usos de `innerHTML`/`title`, 0 con escape). Mismo `esc()` aplicado a campos de texto. *TEST*: nombre de fondo con markup se escapa. *Riesgo*: bajo.
- **E3 · `SECTORS` duplicado Python↔JS** — `on_classification.py:32-42` (fuente) vs `on.js:9-19`/`docs/mockups/on/_shared/sectors.js` (copia baked, sincronización manual). *Enfoque*: agregar `sectors_meta: [{key,short,color,icon}]` al payload de `/on/data` (derivado de `on_classification.SECTORS`, orden canónico); `on.js` puebla `window.ON_SECTORS` desde `ON.DATA.sectors_meta` en el boot, con la copia baked como **fallback** de arranque. Mismos hex/icon/orden. **Agregar la clave `sectors_meta` NO rompe el invariante 14** (es aditiva; las existentes intactas). *TEST*: el cliente y el panel SSR muestran el mismo sector/color por bono. *Riesgo*: medio (orden de boot). *Rollback*: volver a la copia baked.
- **E4 · Dead code shipado en `on.js`** — `on.js:144-164` `header()` con NAV `href="#"` y badge `BYMA open (20m)` fijo, nunca llamado (la página usa `base.html`). Borrar de la fuente compartida (`util.js`) o excluir en el build (como `initTheme()` vía `_require/replace`); regenerar. Verificar que ningún mockup standalone lo use. *Riesgo*: bajo.
- **E5 · Helper de modal/drawer accesible (focus-trap + restore + Esc) + cierre en error** *(incluye el hallazgo "drawer close on error")* — `on.html:214-225`/`on.js:1457-1460` (modal TIR-vs-MD), `#modal` del dashboard, drawer CER de `base.html` no atrapan Tab ni restauran foco. Mini-helper vanilla compartido cableado en `openUniModal`/`closeUniModal` (`on.js:806-814`), `cerOpen`/`cerClose`, el `#modal`. **Además**: asegurar que un fetch/render fallido CIERRA el drawer/modal (o muestra estado de error) en vez de dejarlo colgado abierto y vacío. Mantener cierre por backdrop+Esc. *TEST*: smoke/test de cliente — Esc cierra, foco restaurado; un error de fetch no deja el drawer abierto sin contenido. *Riesgo*: bajo.
- **E6 · (opcional) Refresco SSE inconsistente** — `on.js:1542` usa `EventSource` crudo vs `hx-ext=sse` del dashboard. Documentar/unificar si se aborda; preservar el estado del usuario (filtros/orden/sectores) entre re-fetch. No quitar el gate `#uni-results` de `uni_filters.js:154-158`. **Respetar invariante 16 (nunca despertar SSE en error).** *Riesgo*: bajo (no prioritario).

### EPIC F — TESTS / COBERTURA / GATE

**Objetivo**: estabilizar la red (que el gate sea verde de verdad) y endurecer el aislamiento.

- **F1 · Gate rojo ~33% de los días: `test_cer_vtec_index_date_shifts_with_settle_lag` usa `date.today()` crudo** — `test_settlement_consonance.py:67-96` (`assert v24 != vci` en `:93`, con `date.today()` en `:72/:75`, sin import de `tests._clock`). Cuando `settlement_byma_date(d,0)==settlement_byma_date(d,1)` (122/365 días), `cer_reference_date` colapsa y V.Téc es idéntica → `1 failed` sin cambio de código. `MONITOR_AS_OF` NO lo arregla (el test no importa `tests._clock`). *Enfoque*: `from tests._clock import ref_date` y usar una fecha donde el shift NO colapsa (243/365 válidos), o reformular el `assert v24 != vci` (redundante con los asserts de `idx.calls` de `:95-96` que ya prueban la consonancia real). **No tocar el motor ni `conventions.py`** — el colapso T+0/T+1 es correcto. *Aceptación*: el gate es verde todos los días; el test sigue probando la consonancia precio↔descuento. *Riesgo*: bajo.
- **F2 · Assert tautológico de celda TIR** — `test_panels_router.py:66-67`: `next(...)` sin default + `assert tir_cell is not None` (no puede fallar). Cambiar a `next(..., None)` + `assert tir_cell is not None and tir_cell['text']=='10.00%'`. *Riesgo*: nulo.
- **F3 · `pytest-randomly` con seed fija (opcional, medio)** — agregar a requirements + `[tool.pytest.ini_options]` con `addopts` que fije la seed (reproducible). Primero correr con varias seeds y arreglar las dependencias de orden. La fecha fija de `_clock.py` sigue mandando (randomly NO randomiza fechas). *Riesgo*: medio. *Rollback*: quitar el plugin.
- **F4 · Aislar la fixture module-scope de `test_catalog_repository.py` (tarea propia, prereq de F3)** — `test_catalog_repository.py:26-33` re-siembra la session-DB con `allow_drop=True`, lo que crea acoplamiento de orden y toca la DB de sesión compartida. *Enfoque*: migrar esa fixture a `tmp_db` (DB temporal por módulo/función, sin `allow_drop` sobre la session-DB), siguiendo el patrón de aislamiento de `conftest.py` (memoria `test-db-isolation`). *DoD propio*: el módulo corre aislado, no deja residuo en la session-DB, y los tests pasan en cualquier orden. *TEST*: correr `test_catalog_repository.py` solo y junto al resto en orden aleatorio → idéntico resultado. *Riesgo*: medio. *Rollback*: revertir la fixture.
- **F5 · Tests de los bugs de los epics A-E** — cada tarea de A/B/C/E trae su test rojo→verde (ya enumerados). Asegurar cobertura de routers nuevos/tocados (`test_on_router.py`, `test_panels_router.py`).

### EPIC G — DEUDA TÉCNICA / DEAD CODE

**Objetivo**: cerrar trabajo a medias y limpiar muerto, decidiendo (no dejando a medias).

> **GUARDRAIL DE LIMPIEZA — re-exports load-bearing que PARECEN muertos pero NO lo son.** Antes de borrar cualquier import/símbolo "sin uso aparente", confirmá que no sea un re-export consumido por otro módulo: `conventions.py:15-18` (`days_30_360`, `es_habil`/`is_habil`/`settlement_byma`/`settlement_byma_date`, todos `# noqa: F401`), `services.py:26` (`_cer_reference_date`, lo importa `bond_detail.py:725`), `services.py:39` (`_is_cer_type`, lo importa `bond_detail`). `ruff F401` los exime por el `# noqa`; **NO los quites** — romperían imports de `bond_detail`/dominio. Grepeá el símbolo en todo el repo antes de borrarlo.

- **G1 · `avg_volumes()` read-path muerto** — `price_history.py:140`: el store acumula `volume` cada 5s para siempre pero ningún consumidor de prod lo lee (la col "prom" se sacó de la UI; `_attach_volumes` en `abm.py:92` usa el hub, no `avg_volumes`). **Decidir**: borrar el método + sus 4 tests (`test_price_history.py:57,76,87`), o recablear la col "prom" al blotter. No tocar `get_series`/`record_live_closes`/`record_closes` (alimentan las variaciones). La migración aditiva `volume` no se dropea (forward-only). *Riesgo*: bajo.
- **G2 · CSS muerto col "prom"** — `catalogo.html:167-171` (`uni-grp--mp`/`uni-ccol--mp`/`th:nth-child(10)`) y `abm.html:205` (`th:nth-child(10)`): el thead real (`fragments/abm_universe.html:21-27`) tiene 9 columnas. Borrar los selectores muertos. Los `nth-child` del thead real (1,4,7, separadores) **sí se usan** — sobreviven. *Riesgo*: bajo.
- **G3 · CSS `.uni-*` duplicado verbatim** — `abm.html:185-214` vs `catalogo.html:155-184` (~40 líneas, ya causó drift de los `--mp` muertos). Mover a `app.css` (o un partial `<style>` como `fragments/uni_filters_script.html`) y quitar de los dos `<style>` inline. Ambas páginas renderizan el mismo `abm_universe.html` con `uni_filters.js`. Verificar el blotter en `/abm` y `/catalogo`. *Riesgo*: medio (visual). *Rollback*: revertir el CSS.
- **G4 · Estado del working tree (proceso, no commitear sin pedido)** — feature ON entera + mockups + libs vendoreadas sin commit (`??`), deleciones de "Data912 discovery" (`abm_data912.html`, `test_data912_discovery.py`) sin stage (`D`). NO resucitar discovery (reemplazo: ＋Alta del Universo BYMA, `abm.py:189`). **Solo cuando el usuario lo pida**: stagear las deleciones y cerrar el conjunto ON. Mientras tanto, dejarlo documentado. *Riesgo*: proceso.

---

## 6. ORDEN DE EJECUCIÓN Y DEPENDENCIAS

1. **Primero EPIC F1+F2** (estabilizar la red): si el gate falla 1 de cada 3 días, no podés distinguir regresión de fragilidad. Arreglar el test date-fragile antes de cualquier otra cosa.
2. **EPIC A (Correctitud)**: los bugs que producen datos malos o pérdida de datos. A1 (restore) y A3/A4 (floor/stale) son los más severos. A7/A8/A11 son **verificación primero** (no tocar si no hay bug confirmado). A9 es display, con el guardrail de NO romper el fit de curva. Cada uno con su test.
3. **EPIC B (Robustez)**: depende de A1/A2 (mismo módulo `backup.py`) — hacelo en el mismo branch que A1/A2 si tocan los mismos archivos, o secuenciado después.
4. **EPIC C (Performance)**: C6 (inyección de providers) habilita C4 (cache CI, respetando el single-writer de AppState) y testeo limpio. C2 (índices) habilita C1. C7 es oportunidad opcional. Sin red de tests verde (paso 1) no medís regresiones.
5. **EPIC D + E (Calidad/Frontend)**: D1 (extraer panels_rows) facilita C1/C4 (menos archivo-god). E1/E2 (XSS) son seguridad — alta prioridad dentro de frontend; van cuando la red está verde. F4 (aislar la fixture) es prereq de F3 si se adopta randomly.
6. **EPIC G (Deuda/limpieza)**: al final, cuando todo lo anterior está verde, para no mezclar limpieza con cambios de comportamiento. **Respetar el guardrail de re-exports load-bearing.**

**Dependencias duras**: nada toca el motor congelado. Cualquier epic que toque `price_history`/`fci_history`/`backup`/schema debe pasar por migración aditiva (forward-only). Frontend `on.js` siempre vía fuente + rebuild. Cualquier campo nuevo en `AppState` respeta el single-writer (invariante 17). Ningún path de error notifica SSE (invariante 16).

---

## 7. DEFINITION OF DONE

**Global (toda tarea, sin excepción)**:
- [ ] Test rojo→verde escrito (TDD) y commiteado con el fix (o, si es verificación, decisión de no-cambio documentada con el ancla verificada).
- [ ] `tests/test_pricing_equivalence.py` **verde**.
- [ ] `tests/test_balanz_golden.py` **verde**.
- [ ] `pwsh scripts/check.ps1` **verde** (ruff + pytest completo, exit 0).
- [ ] **Cero regresión de paneles** (shape de `/on/data`, `/fci/data`, `panel_rows.html` intacto; claves nuevas solo aditivas).
- [ ] Ningún invariante de la Sección 2 violado.
- [ ] Sin server colgado; sin commit/push sin pedido.

**Por epic** (además del global):
- **A**: el bug ya no se reproduce; el dato corregido reconcilia con la calculadora Balanz cuando aplica; las tareas de verificación (A7/A8/A11) cerradas con ancla o con fix demostrado por test rojo.
- **B**: restore/backup verificados con test; doc alineada con la implementación; negative-cache/offline-fallback no debilitados.
- **C**: medición antes/después (p.ej. nº de full-scans, nº de `execute()` del motor por ciclo y por request) documentada; frescura preservada; AppState single-writer respetado.
- **D**: comportamiento idéntico (HTML byte-equivalente donde aplique); deuda removida sin cambio funcional.
- **E**: XSS cerrado (string editable escapado); fuente única de SECTORS; `on.js` regenerable 1:1; sin CDN; modal/drawer accesible y sin quedar colgado en error.
- **F**: gate verde de forma determinística (no date-fragile); aislamiento de orden si se adopta randomly (F4).
- **G**: decisión tomada (borrar vs cablear), no dejado a medias; ningún re-export load-bearing borrado.

---

## 8. LO QUE NO HACER (ANTI-OBJETIVOS)

- **NO reescribir el motor** ni "mejorar" sus fórmulas. La fachada delega; el legacy congelado manda.
- **NO tocar las firmas públicas** de `FinancialEngine` (`@staticmethod` con `settle_date`/`settle_lag`).
- **NO mover la dolarización de pata ARS soberana al motor** (rompe la equivalencia).
- **NO poner `verify=False` global** ni habilitar `follow_redirects` cross-host con el cliente no-verify.
- **NO re-seed destructivo sin guard**; `reload()` jamás re-siembra; nada de drop/recreate de schema.
- **NO meter venv/.db dentro del proyecto** (OneDrive); `.db` y backups en `%LOCALAPPDATA%\monitor`.
- **NO cambiar el shape de `/on/data` ni `/fci/data`** ni las escalas documentadas (tir/parity ×100, change_pct ya en %); las claves nuevas solo aditivas.
- **NO editar `on.js` a mano** (auto-generado); siempre fuente + `build_on_static.py`.
- **NO reintroducir `<script src=...CDN...>`**: las libs JS (htmx, chart.umd, html2canvas, gridstack, htmx-ext-sse) se sirven SIEMPRE locales desde `static/vendor/` (`base.html:11-14`/`index.html:5`). Un CDN caído rompería la página entera.
- **NO borrar los re-exports load-bearing que parecen muertos**: `conventions.py:15-18` (`days_30_360`, `settlement_byma*`, `es_habil`/`is_habil`), `services.py:26` (`_cer_reference_date`, usado por `bond_detail.py:725`), `services.py:39` (`_is_cer_type`). Tienen `# noqa: F401` a propósito; quitarlos rompe imports de `bond_detail`/dominio.
- **NO silenciar errores con `pass`/`except Exception` ancho** (la política es "NUNCA tragar el error"; `_safe_synth` acota a `ValueError/KeyError/TypeError` a propósito).
- **NO ampliar el except de `_safe_synth` a `Exception`** (guardaría altas con cero cashflows en silencio).
- **NO notificar SSE desde el path de error** (`record_error` no despierta paneles — anti request-storm, invariante 16).
- **NO construir `Instrument` con `model_construct`** (bypassearía el sort de cashflows; invariante 19). Las altas/synth pasan por el constructor normal.
- **NO usar `date.today()` crudo** en pricing core ni en tests date-sensitive (anclar a `clock.today()`/`tests._clock.ref_date()`).
- **NO degradar el stale-safe del hub** a un clear total, ni invertir la precedencia activa>floor, ni convertir un `CircuitOpenError`/breaker abierto en una excepción que tumbe `refresh_all` (invariante 18).
- **NO "arreglar" los `not m.duration or m.duration <= 0`** de `panels.py:115/:624`: duration 0.0 es inválida para `math.log` en el fit de curva — cambiarlos rompe el fit.
- **NO abordar la migración de los providers sync a `ResilientClient` / retirar `_http.py`**: está DIFERIDA a propósito (cola opcional de `CLAUDE.md`) — corren en `to_thread` con cache+TTL propio; tocarlos es alto riesgo / bajo valor y puede romper el read-path. No la tomes por iniciativa.
- **NO dejar el server uvicorn colgado**; **NO commitear/pushear sin pedido**.

---

## 9. PROTOCOLO DE ENTREGA

- **1 branch + 1 PR por epic**. Naming: `fix/<epic>-<slug>` (correctitud/robustez), `perf/<slug>`, `refactor/<slug>` (calidad), `chore/<slug>` (deuda). Ej.: `fix/restore-target-rotation`, `perf/abm-count-unloaded`, `refactor/extract-panels-rows`.
- **Branch primero** si estás en `main`/`master`. No trabajar directo sobre la rama default.
- **Commits** en español, atómicos por tarea, mensaje con qué/por qué + referencia a archivo:línea del bug. Terminar con la línea Co-Authored-By.
- **Artefactos Superpowers** en `docs/superpowers/`: `specs/<epic>.md` (qué/por qué/invariantes en juego) y `plans/<epic>.md` (tareas TDD, comandos con `py -3.12`, criterios de aceptación). Un epic = un spec + un plan.
- **Body del PR**: resumen del epic, lista de tareas con archivo:línea, tests agregados, evidencia de gate verde (output de `scripts/check.ps1`), evidencia de equivalence+golden verdes, riesgos y rollback. Terminar con la línea "Generated with Claude Code".
- **Reporte de resultados** (en tu salida, no en archivos `.md` sueltos): qué corriste, qué pasó/falló con números (nº de tests, antes/después de performance), y los file:line de cada cambio. Si algo quedó rojo o bloqueado por un invariante, decilo explícito con el motivo.

---

## 10. CHECKLIST FINAL (antes de cerrar la campaña)

- [ ] `pwsh scripts/check.ps1` verde de forma **determinística** (corrido en ≥2 fechas distintas / con `MONITOR_TEST_REF_DATE` variado — el gate ya no es date-fragile).
- [ ] `test_pricing_equivalence.py` + `test_balanz_golden.py` + `test_daycount_pricing.py` + `test_xirr_solver.py` + `test_pricing_invariants.py` **verdes**.
- [ ] Los 15 hallazgos del review **resueltos o explícitamente diferidos con motivo**: restore-target (A1), floor-zero-symbol (A3/A4), NaN/Inf (A5), `_safe_synth`/AttributeError→500 (A6), HARD DOLLAR prefill (A7), cupón amortizing (A8), falsy-zero TIR (A9), fci_history continuidad (A10), intradía-como-cierre (A11), XSS `on.js` (E1), XSS `fci.js` (E2), drawer/modal close-on-error + a11y (E5), SECTORS duplicado (E3), backup/restore (A1/A2/B1), gate date-fragile (F1).
- [ ] Ningún invariante de la Sección 2 violado (verificado: equivalencia, firmas, day-count por instrumento, ex-cupón estricto, SQLite=verdad, forward-only, TLS verify, clock, OneDrive, `py -3.12`, **record_error sin SSE, AppState single-writer, CircuitOpenError no tumba ciclo, cashflows cronológicos / sin model_construct**).
- [ ] Shape de `/on/data` y `/fci/data` y de los fragmentos SSR **intacto** (claves nuevas solo aditivas; clientes JS no rotos).
- [ ] Migraciones de schema **aditivas** (ningún drop); `reload()` no re-siembra.
- [ ] `on.js` **regenerado** desde fuente (no editado a mano); fuentes en `docs/mockups/on/_shared/`; **sin CDN** (libs locales en `static/vendor/`).
- [ ] **Ningún re-export load-bearing borrado** (`conventions.py:15-18`, `services._cer_reference_date`/`_is_cer_type`).
- [ ] Migración de providers sync a `ResilientClient` **NO abordada** (diferida a propósito).
- [ ] Sin venv/.db en el repo; sin server colgado; sin commits/pushes no solicitados.
- [ ] Cada epic con su PR, spec y plan en `docs/superpowers/`, gate verde evidenciado en el body.
- [ ] Reporte final con evidencia (qué corrió, qué falló, números antes/después, file:line de los cambios).
