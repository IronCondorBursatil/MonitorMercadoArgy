# Arquitectura — mapa del codebase (post-reingeniería)

Movido desde `CLAUDE.md` en la Fase 4 (agents.md §0.8). Es el mapa completo: qué vive
dónde y por qué. Las **reglas** (invariantes) siguen en `CLAUDE.md`; las convenciones
financieras en `docs/convenciones-financieras.md`; la capa web en detalle en
`docs/flujo-web.md`; el despliegue en `docs/despliegue.md`.

## Qué es

Monitor de renta fija argentina (Soberanos, CER, Tasa Fija, TAMAR/Dual, Dólar Linked,
Bopreales, ONs, Provinciales, Valor Relativo, Futuros DLR, BEI, Panel Líder) + FCI +
Opciones + Catálogo BYMA + Cartera + ABM.

**Precios: BYMA open** (default `settings.market_source="byma_open"`; alternativas
`byma_realtime` y `data912`, conmutables EN CALIENTE desde `/source/*` o por
`MONITOR_MARKET_SOURCE`) **con floor Data912 mergeado DEBAJO**: cada ciclo, si la
activa no es Data912, el hub trae un snapshot Data912 cacheado y rellena con él los
símbolos que la activa no lista —y pisa un precio 0 de la activa con un cierre real,
porque un 0 no es dato (ver `provider_hub._apply_floor`)—. O sea: la quote de una fila
puede venir de `byma/field_map.byma_row_to_quote` **o** del floor, por símbolo y por
ciclo; si un precio sale raro, mirar las dos. Índices de **BCRA**, futuros de
**Matba/Rofex WS**, FX de **dolarapi**, FCI de **CAFCI + ArgentinaDatos**,
calificaciones de **FIX SCR**, letras nuevas de **ArgentinaDatos**.

`run.py` arranca **uvicorn** sobre `apps/web/app.py:app` (FastAPI). Config en
`config/settings.py` (`settings`, pydantic-settings; override por env `MONITOR_*`).
Las `.db` viven en `settings.db_dir` (fuera del working tree de git; ver `docs/despliegue.md`).

## Árbol

```
core/domain/
  models.py            Pydantic v2: Cashflow/Instrument (frozen) + MarketSnapshot. InstrumentMetrics=dataclass.
  services.py          FinancialEngine — FACHADA delgada (preserva firmas) que delega al pricing core.
  xirr.py conventions.py daycount.py   funciones puras (XIRR act/365.25 Brent, 30/360, tasas, settlement, tamar_tem, cer_ref; day-count DECLARADO por bono para descontar).
  clock.py currency.py   `today()` del dominio (overrideable por MONITOR_AS_OF, solo tests) · moneda por sufijo D/C/— (fuente única).
  instrument_groups.py   universo de `instrument_type` VÁLIDOS por grupo + `is_known_type()` + `ANALYTIC_PAYOFF_TYPES` — ver invariante "tipos" en CLAUDE.md.
  pricing/
    protocols.py context.py   IndicesProvider/FxProvider (Protocol) + PricingContext (inmutable).
    base.py            VanillaStrategy: camino general (vanilla + CER inline + 30/360 inline + LECAP).
    strategies.py      Cer / DolarLinked / Tamar / DualCerTamar (overrides + super() fallback).
    registry.py        strategy_for(inst) — tabla predicado→strategy (mata la escalera if/elif).
    metrics.py tamar.py stubs.py fx_legs.py   métricas popup / payoff BONTE TAMAR + V.Téc dual (CONTRATO en el docstring de tamar.py) / ZeroTamar / pata pesos→USD solo para display.
  on_cashflows.py on_classification.py   cashflows USD de ONs (el capital amortiza con su propio factor) + sector del emisor por keywords.
  options/     opciones BYMA, puro y testeable: symbols.py + roots.py (parser de ticker → ROOT/tipo/strike/mes) · expiry.py (3er viernes, corrido al hábil previo) · pricing.py (CRR americano + solver de IV) · greeks.py (diferencias finitas sobre el árbol) · rates.py (tasas implícitas) · chain.py (builder de la chain enriquecida) · strategies.py + analytics.py (payoff multi-leg, optionlab) · models.py
  cashflow_synth.py portfolio.py scenarios.py yield_curve.py inflation_path.py   (sin cambios)
  fci/         dataset del panel FCI (puro, testeable): derive.py (subcategoría estilo fonditos + AUM join ArgentinaDatos + unificación de clases x fondo) · hist.py (cuotaparte reconstruida de retornos reales / real de fci_history) · lens.py (devaluación A3500 + inflación CER por período) · dataset.py (build_fci_dataset → forma que consume static/js/fci.js). Composición NO se sintetiza; flujos solo reales.
core/holiday_engine.py   calendario BYMA + feriados AR. TODO el settlement cuelga de acá (conventions/pricing/metrics/bond_detail): un feriado mal cargado mueve el V.Téc de cada bono indexado. En runtime es 100 % offline (`data/feriados_ar.xlsx`); `refresh()`/`descargar_todos()` van a la red y REESCRIBEN el xlsx: jamás en tests.
core/infrastructure/
  db/        engine.py (SQLite+WAL, reconfigurable p/ tests) · models.py (ORM 2.0, + sheet/raw_fields del ABM) · catalog_repository.py (CatalogRepository, drop-in del ExcelRepo; reseed_with_meta; `type_health` → /api/health) · backup.py
  async_http.py circuit_breaker.py provider_hub.py   ingesta async (httpx + breaker + pool + semáforo por host). `ProviderHub.refresh_all` mergea la fuente activa con el FLOOR Data912 (`_apply_floor`) y `HubMarketDataProvider` la expone al motor; ambos CABLEADOS al refresh loop.
  _tls.py    política de verificación TLS por host (siempre verifica; allowlist vacía; perilla `MONITOR_TLS_NO_VERIFY_HOSTS`).
  byma/      capa BYMA: sources.py (`MarketSource` byma_open | byma_realtime | data912 + `make_source`, el registry que elige la fuente live) · field_map.py (fila BYMA → `Data912Row`, puro) · credentials.py (BYMADATA_USER/PASS al `.env`, se cargan desde la UI y aplican en caliente) · universe.py (seed `titulos_final.csv` → tabla `byma_catalog` + buscador del ABM) · catalog_products.py (cauciones/índices/SENEBI del tab Catálogo) · catalog_enrich.py (ISIN/emisor/tipo) · chart_history.py + series_historicas.py (cierres diarios; `chart` es el default y es estrictamente mejor) · index_history.py
  schemas.py           Data912Row (validación Pydantic en el borde de ingesta)
  repositories.py data912_provider.py indices_provider.py fx_provider.py futures_provider.py rem_provider.py cafci_provider.py argentinadatos_provider.py bondterminal_provider.py
  on_catalog.py        siembra de ONs desde `data/obligaciones_negociables.csv`. `ingest()` es DESTRUCTIVO (ver CLAUDE.md); su único caller con guard es `apps/web/app.py::_ensure_obligaciones_negociables`. Lo que se llevaría puesto sobre una DB poblada: las ON que viven SOLO en la DB — las de bancos del ABM (BACH 30/360, BF37/BPCV/BYCV/CACB/CICA) y las de `scripts/load_bond.py` / `scripts/ingest_irsa_ons.py`.
  letras_sync.py       planificador PURO del alta automática de letras (qué falta, qué se rechaza y por qué); lo ejecuta `apps/web/letras_service.py`. Las reglas duras de CLAUDE.md salen de lo observado en la API: `fechaEmision` vacía en 12 de 18 letras (no se puede deducir → no hay alta) y letras muertas en el payload (S17A6 seguía listada 5 meses después de vencer).
  price_history.py     store SQLite auto-mantenido de cierres diarios (rendimientos Sem/1M/3M/YTD/1A): priming Data912 /historical/bonds + acumulación del feed vivo; read-path local (merge con el CSV legacy)
  fix_ratings.py       scraper + parser puro del listado FIX SCR (grid Yii2/Kartik, SSR). per-page topea en 50; la paginación corta por filas CRUDAS (una fila descartada NO significa página incompleta). Política por entidad: Emisor > Endeudamiento LP → 125 emisores
  ratings_history.py   store SQLite (fuera del working tree) del corte diario de FIX + diff up/down/watch. Guard: descarta el corte con <60% del MAYOR de los últimos 30 (contra el previo a secas se ratchetea). Diffea contra el ÚLTIMO ESTADO CONOCIDO de cada entidad, no contra el corte anterior (un hueco de un día se tragaba el cambio)
  ratings.py           matcher determinista por emisor (NO fuzzy) + MERGE store-sobre-CSV: `data/calificaciones.csv` es SEMILLA y retiene los emisores que FIX dejó de publicar (Agrality, Metalfor, Mastellone). `as_of()` = fecha del corte vivo
  fci_history.py       store SQLite (fuera del working tree) de vcp/ccp/patrimonio por fondo (ArgentinaDatos), acumulado a diario por el loop → flujo neto real `Δccp × precio de cuotaparte` (`net_flow_series`). El precio sale de `patrimonio/ccp` por fila (fallback `vcp/1000`): ArgentinaDatos publica el VCP **por cada 1.000 cuotapartes**, así que multiplicar por el VCP crudo inflaba el flujo ×1000. `ccp<=0` es DATO AUSENTE (45% del corte), no circulación cero: se descarta y la serie se puentea, si no fabricaba suscripciones/rescates fantasma por el patrimonio entero. cafci_provider._parse_payload conserva los campos ricos de CAFCI (honorarios/horizonte/duration/region/tickers/min/objetivo)
  repositories.build_instrument()   parser de fila → Instrument, COMPARTIDO por el loader Excel y el ABM SQLite; `_resolve_instrument_type` es el único que decide el `instrument_type` (y avisa por WARNING cuando lo asume o queda huérfano)
core/security.py       hash bcrypt + JWT HS256 (create/decode). Ver `docs/auth.md`.
apps/web/
  app.py               FastAPI + lifespan (5 loops supervisados + `_startup_reconcile`; el motor corre vía to_thread). MONITOR_DISABLE_LOOPS en tests. `/api/health` público y recortado.
  state.py deps.py      AppState (snapshot vivo + revision/wait_for_change p/ SSE + `loop_crashes`/`degraded_loops` + salud del catálogo) + Depends (get_repo→CatalogRepository, get_state, get_hub, ...)
  supervisor.py         `supervise()` — reinicia con backoff el loop que termine por lo que sea (ver abajo)
  deps_auth.py security_web.py   login/permisos por pestaña (`RequireTabPermission`) · CSRF por validación de origen + headers de seguridad (`SecurityHeadersMiddleware`, `reject_cross_site`)
  routers/             panels (14 paneles + home; el registro es `PANELS`/`PANEL_ORDER` de `routers/panels_schema.py` — NO fijar el número en otro lado) · bonds (/bond/{t}/detail+metrics, el modal) · on (página + /on/data + PDF) · options (chain/smile/OI/scanner/analytics) · catalog (Catálogo BYMA: índices/cauciones/SENEBI/ficha) · curva · cartera · bcra · cashflows · escenarios · fci (página + /fci/data JSON) · abm · auth (login + rate-limit) · users_abm (admin) · header · source (conmuta la fuente live y guarda las credenciales BYMA) · stream (SSE)
  on_service.py on_pdf.py   dataset de `/on/data` (memoizado por revisión del snapshot + día, espejo del de FCI) + PDF del panel ON listo para cliente
  on_src/              FUENTE de la app cliente de /on (sectors.js + util.js + unified.js + on_app.html) → `scripts/build_on_static.py` genera `static/js/on.js`. Ver invariante "on.js" en CLAUDE.md.
  fci_service.py       junta CAFCI enriquecido + AUM + macro (lens A3500/CER) + flujos (fci_history) → dataset memoizado de /fci/data
  letras_service.py    ejecuta el plan de `letras_sync` escribiendo por `instruments_abm.save_instrument`; audita cada alta (`monitor.audit`).
  templates/           base.html + pages/* + fragments/* (Jinja + HTMX)
  static/css/app.css   diseño propio (light/dark) · static/css/{fci,on,options}.css · static/vendor/gridstack · static/js/fci.js (app cliente del panel FCI: 5 vistas + detalle, Chart.js) · static/js/on.js (AUTO-GENERADO)
  bond_detail.py instruments_abm.py cartera_store.py panels_rows.py templates.py   (reusados por los routers)
apps/cli/bei.py        monitor BEI extendido (NT3/2019 + NT8/2024): acá vive `compute_bei_tables`, que llama el `_bei_loop` del lifespan. `_common.py` = bootstrap del use-case.
run.py scripts/ tests/ data/ config/ deploy/ .claude/ .github/workflows/
```

## Supervisión de los loops

`apps/web/supervisor.py`: los 5 loops del lifespan (`_refresh_loop`, `_options_loop`,
`_bei_loop`, `_price_history_loop`, `_ratings_loop`) van envueltos en `supervise()`, que
los **reinicia** si terminan por lo que sea (excepción, retorno o *cancelación espuria*)
con backoff 1s→60s, y reporta el motivo por `record_error`. Motivo: `asyncio.create_task`
es fire-and-forget — el 2026-09-01 `_refresh_loop` murió mudo a las 12:45
(`except CancelledError: raise`) y la app sirvió el mismo snapshot ~22hs, con los otros
4 loops vivos. El lifespan setea `app.state.stopping` **antes** de cancelar: así el
supervisor distingue el shutdown real de una caída. `_startup_reconcile` NO se supervisa
(corre 1× y terminar es su contrato). El detalle de cada loop está en `docs/flujo-web.md`.

## Observabilidad

Si el refresh loop falla, `AppState.record_error` lo registra y el header lo muestra
(badge `/health/badge` verde/ámbar/rojo) + `/api/health` da `status`/`is_stale`/
`age_seconds`/`degraded_loops`/`loop_crashes_24h`/`catalog{...}` (público, por eso SIN
`last_error` ni tickers). La app sigue sirviendo el último snapshot bueno.

**Severidad por loop** (`state._CRITICAL_LOOPS`): la caída del loop **refresh** es
crítica (badge rojo "sin datos" + `status: degraded`, con retención de 300s para que no
se la coma el ciclo siguiente); la de los laterales (ratings/bei/price_history/options)
es degradación **parcial** — va a `status()["loop_crashes"]` (24hs, con motivo) y a
`degraded_loops` (ventana de 300s, sólo nombres, también en `/api/health`), sin apagar
el semáforo de unos precios que están frescos: el badge la muestra en **ámbar** ("loop
caído: <nombre>", motivo en el tooltip — el badge está detrás de login), nunca en rojo
"sin datos". El supervisor reporta la caída con el nombre del loop **estructurado**
(`record_loop_crash(name, reason)`), no parseando el texto del mensaje.

Afuera de la app, `.github/workflows/staleness.yml` consulta `/api/health` cada hora y
falla (email de GitHub) si `is_stale`, `status != "ok"` o `loop_crashes_24h > 0` — ver
`docs/despliegue.md`.

## Timeouts de httpx (regla dura, también en CLAUDE.md)

El centinela de "usar el timeout del cliente" es `httpx.USE_CLIENT_DEFAULT`, **NO
`None`**. `None` significa `httpx.Timeout(None)`, o sea SIN connect/read/write/pool
timeout: un request colgado no vuelve nunca y el loop que lo espera queda awaiteando para
siempre (fue el bug latente más viejo del repo, ~100 días, y causó el incidente del
2026-09-01). `ResilientClient` ya usa el centinela correcto (`async_http._USE_CLIENT_DEFAULT`);
cualquier wrapper nuevo tiene que copiarlo, no "simplificar" a `None`. Guarda:
`tests/test_aud_A_infra_http_timeouts.py`.

## Salud del catálogo

`CatalogRepository.type_health` → `AppState.set_catalog_health` → bloque `catalog` de
`/api/health` (cuentas de `instruments`/`orphans`/`defaulted`/`seed_failed`, no tickers).
Un `instrument_type` fuera de `instrument_groups.py` deja el bono invisible (ver el
invariante en CLAUDE.md); las filas ya dañadas se arreglan con
`scripts/migrate_orphan_types.py` (dry-run por default).
