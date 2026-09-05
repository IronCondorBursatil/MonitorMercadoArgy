# CLAUDE.md — Guía del codebase (post-reingeniería)

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
calificaciones de **FIX SCR**.

> Este archivo describe la arquitectura **actual** (post-reingeniería).
> `agents.md` conserva las **convenciones financieras** (CER NT8/2024, TAMAR, BEI,
> day-counts, MD BYMA) que siguen 100% vigentes, y su stack/pilares/`.env` se pusieron al
> día en la auditoría 2026-09 — pero su descripción de la capa **web** sigue siendo vieja
> (era http.server + SPA `app.js`, con `_get_columns`/`Snapshot.__init__`/Gridstack y los
> endpoints `/api/*`). Para todo lo que sea web, la verdad es la de acá.

## Cómo correr

```powershell
# Python 3.12 del sistema (el de Programs; `py -3.12` resuelve a él). El antiguo
# "Microsoft Store Python" ya NO existe en esta máquina — ver memoria env_python_interpreter.
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"   # o simplemente: py -3.12
& $py -m pip install -r requirements.lock -r requirements-dev.txt   # runtime + gate
# (prod instala SOLO requirements.txt: pytest/hypothesis/ruff no van al servidor)
$env:MONITOR_ADMIN_PASSWORD='...'; & $py scripts/init_admin.py   # SOLO la 1ª vez (ver abajo)
& $py run.py                          # levanta uvicorn → http://localhost:8000
& $py -m pytest tests/ -q             # tests (~2330)
pwsh scripts/check.ps1                # GATE: ruff + pytest (antes de pushear a main)
& $py scripts/ingest_master.py        # Excel → SQLite (cuando editás el master a mano)
```

`run.py` arranca **uvicorn** sobre `apps/web/app.py:app` (FastAPI). Config en
`config/settings.py` (`settings`, pydantic-settings; override por env `MONITOR_*`).
Las `.db` viven en `%LOCALAPPDATA%\monitor` (**fuera del working tree de git**).

**Primer arranque en una máquina/servidor nuevo**: la app entera está detrás de login y
NO hay bootstrap automático de usuarios (ni el lifespan ni `deploy.sh` crean el admin).
Con un `db_dir` virgen, `/login` renderiza bien y responde "Usuario o contraseña
incorrectos" para siempre — un callejón silencioso, no un 500. Correr
`scripts/init_admin.py` con `MONITOR_ADMIN_PASSWORD` seteado ANTES del primer `run.py`.
En la máquina del autor no se nota: la `catalog.db` vive fuera del árbol, así que un
`git clone` fresco reusa el admin que ya existe.

## Arquitectura

```
core/domain/
  models.py            Pydantic v2: Cashflow/Instrument (frozen) + MarketSnapshot. InstrumentMetrics=dataclass.
  services.py          FinancialEngine — FACHADA delgada (preserva firmas) que delega al pricing core.
  xirr.py conventions.py daycount.py   funciones puras (XIRR act/365.25 Brent, 30/360, tasas, settlement, tamar_tem, cer_ref; day-count DECLARADO por bono para descontar).
  clock.py currency.py   `today()` del dominio (overrideable por MONITOR_AS_OF, solo tests) · moneda por sufijo D/C/— (fuente única).
  instrument_groups.py   universo de `instrument_type` VÁLIDOS por grupo + `is_known_type()` — ver invariante "tipos".
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
core/holiday_engine.py   calendario BYMA + feriados AR. TODO el settlement cuelga de acá (conventions/pricing/metrics/bond_detail): un feriado mal cargado mueve el V.Téc de cada bono indexado.
core/infrastructure/
  db/        engine.py (SQLite+WAL, reconfigurable p/ tests) · models.py (ORM 2.0, + sheet/raw_fields del ABM) · catalog_repository.py (CatalogRepository, drop-in del ExcelRepo; reseed_with_meta; `type_health` → /api/health) · backup.py
  async_http.py circuit_breaker.py provider_hub.py   ingesta async (httpx + breaker + pool + semáforo por host). `ProviderHub.refresh_all` mergea la fuente activa con el FLOOR Data912 (`_apply_floor`) y `HubMarketDataProvider` la expone al motor; ambos CABLEADOS al refresh loop.
  byma/      capa BYMA: sources.py (`MarketSource` byma_open | byma_realtime | data912 + `make_source`, el registry que elige la fuente live) · field_map.py (fila BYMA → `Data912Row`, puro) · credentials.py (BYMADATA_USER/PASS al `.env`, se cargan desde la UI y aplican en caliente) · universe.py (seed `titulos_final.csv` → tabla `byma_catalog` + buscador del ABM) · catalog_products.py (cauciones/índices/SENEBI del tab Catálogo) · catalog_enrich.py (ISIN/emisor/tipo) · chart_history.py + series_historicas.py (cierres diarios; `chart` es el default y es estrictamente mejor) · index_history.py
  schemas.py           Data912Row (validación Pydantic en el borde de ingesta)
  repositories.py data912_provider.py indices_provider.py fx_provider.py futures_provider.py rem_provider.py cafci_provider.py argentinadatos_provider.py bondterminal_provider.py
  price_history.py     store SQLite auto-mantenido de cierres diarios (rendimientos Sem/1M/3M/YTD/1A): priming Data912 /historical/bonds + acumulación del feed vivo; read-path local (merge con el CSV legacy)
  fix_ratings.py       scraper + parser puro del listado FIX SCR (grid Yii2/Kartik, SSR). per-page topea en 50; la paginación corta por filas CRUDAS (una fila descartada NO significa página incompleta). Política por entidad: Emisor > Endeudamiento LP → 125 emisores
  ratings_history.py   store SQLite (fuera del working tree) del corte diario de FIX + diff up/down/watch. Guard: descarta el corte con <60% del MAYOR de los últimos 30 (contra el previo a secas se ratchetea). Diffea contra el ÚLTIMO ESTADO CONOCIDO de cada entidad, no contra el corte anterior (un hueco de un día se tragaba el cambio)
  ratings.py           matcher determinista por emisor (NO fuzzy) + MERGE store-sobre-CSV: `data/calificaciones.csv` es SEMILLA y retiene los emisores que FIX dejó de publicar (Agrality, Metalfor, Mastellone). `as_of()` = fecha del corte vivo
  fci_history.py       store SQLite (fuera del working tree) de vcp/ccp/patrimonio por fondo (ArgentinaDatos), acumulado a diario por el loop → flujo neto real `Δccp × precio de cuotaparte` (`net_flow_series`). El precio sale de `patrimonio/ccp` por fila (fallback `vcp/1000`): ArgentinaDatos publica el VCP **por cada 1.000 cuotapartes**, así que multiplicar por el VCP crudo inflaba el flujo ×1000. `ccp<=0` es DATO AUSENTE (45% del corte), no circulación cero: se descarta y la serie se puentea, si no fabricaba suscripciones/rescates fantasma por el patrimonio entero. cafci_provider._parse_payload conserva los campos ricos de CAFCI (honorarios/horizonte/duration/region/tickers/min/objetivo)
  repositories.build_instrument()   parser de fila → Instrument, COMPARTIDO por el loader Excel y el ABM SQLite; `_resolve_instrument_type` es el único que decide el `instrument_type` (y avisa por WARNING cuando lo asume o queda huérfano)
apps/web/
  app.py               FastAPI + lifespan (5 loops supervisados + `_startup_reconcile`; el motor corre vía to_thread). MONITOR_DISABLE_LOOPS en tests.
  state.py deps.py      AppState (snapshot vivo + revision/wait_for_change p/ SSE + `loop_crashes`/`degraded_loops` + salud del catálogo) + Depends (get_repo→CatalogRepository, get_state, get_hub, ...)
  supervisor.py         `supervise()` — reinicia con backoff el loop que termine por lo que sea (ver Robustez)
  routers/             panels (14 paneles + home; el registro es `PANELS`/`PANEL_ORDER` de panels_schema.py — NO fijar el número acá) · bonds (/bond/{t}/detail+metrics, el modal) · on (página + /on/data + PDF) · options (chain/smile/OI/scanner/analytics) · catalog (Catálogo BYMA: índices/cauciones/SENEBI/ficha) · curva · cartera · bcra · cashflows · escenarios · fci (página + /fci/data JSON) · abm · auth (login + rate-limit) · users_abm (admin) · header · source (conmuta la fuente live y guarda las credenciales BYMA) · stream (SSE)
  on_service.py on_pdf.py   dataset de `/on/data` (memoizado por revisión del snapshot + día, espejo del de FCI) + PDF del panel ON listo para cliente
  on_src/              FUENTE de la app cliente de /on (sectors.js + util.js + unified.js + on_app.html) → `scripts/build_on_static.py` genera `static/js/on.js`. Ver invariante "on.js".
  fci_service.py       junta CAFCI enriquecido + AUM + macro (lens A3500/CER) + flujos (fci_history) → dataset memoizado de /fci/data
  templates/           base.html + pages/* + fragments/* (Jinja + HTMX)
  static/css/app.css   diseño propio (light/dark) · static/css/{fci,on,options}.css · static/vendor/gridstack · static/js/fci.js (app cliente del panel FCI: 5 vistas + detalle, Chart.js) · static/js/on.js (AUTO-GENERADO)
  bond_detail.py instruments_abm.py cartera_store.py panels_rows.py templates.py   (reusados por los routers)
apps/cli/bei.py        monitor BEI extendido (NT3/2019 + NT8/2024): acá vive `compute_bei_tables`, que llama el `_bei_loop` del lifespan. `_common.py` = bootstrap del use-case.
run.py scripts/ tests/ data/ config/
```

## Flujo web (HTMX SSR)

`run.py`→uvicorn→`app.py`. El **lifespan** arranca **5 loops supervisados** (los mismos 5
que envuelve `supervise()`, ver Robustez) más `_startup_reconcile` (corre 1× y termina —
por eso NO se supervisa):

- `_refresh_loop` (`settings.refresh_sec`, 5s): `await hub.refresh_all()` trae la **fuente
  live activa** (`byma_open` por default) async con breaker+pool y le mergea el floor
  Data912; después el motor corre `GenerateMonitorReport.execute` vía `to_thread` leyendo
  el snapshot ya materializado → `AppState`. Es el único loop **crítico**.
- `_options_loop` (`settings.options_refresh_sec`, 60s, + una corrida al arranque): chain
  de opciones — parser + CRR + griegos de ~1000 contratos, 5-20s. Va en loop propio a
  propósito: adentro del refresh lo llevaba a ~25s y espaciaba el push SSE de los bonos.
  De ahí la asimetría 60s vs 5s que se ve en el panel de Opciones.
- `_bei_loop` (`bei_refresh_sec`, 300s): `apps.cli.bei.compute_bei_tables`.
- `_price_history_loop` (`price_history_sec`, 1h): mantiene el store de cierres diarios
  para los rendimientos (priming Data912 `/historical/bonds` + acumulación del feed),
  **acumula el corte diario de ArgentinaDatos en `fci_history`** (flujos del FCI), toma
  el backup periódico del catálogo y **da de alta las letras nuevas** (ver abajo).
- `_ratings_loop`: 1 corte por día de FIX SCR (si ya está el de hoy no re-scrapea; tras un
  corte nuevo invalida el cache de `ratings`).

Cada panel es un `<tbody hx-get="/panels/{id}/rows">` que renderiza un fragmento SSR desde
`AppState`; el auto-refresh es **event-driven por SSE** (`/stream` pushea `refresh` por
ciclo). El fallback por polling es `every 60s` —no 15s: se subió en el hardening de
realtime porque duplicaba el push del SSE— y **todos** los triggers van gateados por
`[mrRefreshOK(this)]` (no refrescar con la pestaña oculta / un modal abierto) más
`tabvisible from:body` para repintar al volver. Los 15s que quedan en `base.html` son el
poll del badge `/health/badge`, no los paneles. El detalle es un modal
(`/bond/{t}/detail` + `/bond/{t}/metrics`).

**Paneles FCI y ON** (las dos excepciones al SSR): sirven una página que carga una app
cliente vanilla y ésta hace `fetch` de su dataset JSON.
`GET /fci` → `static/js/fci.js` (5 vistas + detalle con Chart.js) → `/fci/data`, que arma
`fci_service.get_fci_dataset` (memoizado por corte/día, GZip) combinando CAFCI
enriquecido + AUM ArgentinaDatos + lente A3500/CER + flujos reales de `fci_history`.
`GET /on` → `static/js/on.js` (**auto-generado**, ver invariante) → `/on/data`, que arma
`on_service` desde el snapshot vivo clasificando por sector al vuelo (+ `/on/pdf`).

## Autenticación y permisos

Toda la app está detrás de login (`apps/web/deps_auth.py` + `routers/auth.py` + `core/security.py`).
JWT en cookie httponly (`access_token`), firmado HS256. **El secreto NO es hardcodeado**: se
resuelve en `settings.model_post_init` → env `MONITOR_JWT_SECRET_KEY` > archivo `db_dir/jwt_secret`
(0600, fuera del working tree) > generado y persistido al vuelo. En prod, setear `MONITOR_JWT_SECRET_KEY`.

Permisos por pestaña: `UserORM.allowed_tabs` (JSON) + `RequireTabPermission("<tab>")` como
`dependencies=` de cada router en `app.py`. `is_admin` bypasea; `"*"` = todas. Los routers de
lectura global (`header`, `source`, `stream`) van con `get_current_user_html` (solo login);
`/source/*` POST (conmutar la fuente, guardar credenciales BYMA) y `/users/*` exigen **admin**. `/api/health` es público pero recortado (sin `last_error`).

Falta de **permiso** ≠ falta de **login**: `RequireTabPermission` levanta
`TabForbiddenException` → **403** con la lista de pestañas habilitadas (`deps_auth.py` +
handler en `app.py`). Sólo el no-autenticado (`RequiresLoginException`) va a 302 `/login`.

**Rate-limit del login** (`routers/auth.py`): 5 intentos / 5 min por (IP, usuario). La IP
sale del peer TCP y sólo se cree el `X-Forwarded-For` si ese peer está en
`settings.trusted_proxy_ips` (default `127.0.0.1,::1`; override `MONITOR_TRUSTED_PROXY_IPS`,
lista por comas, vacío = no confiar en ningún XFF). Supone **UN** solo proxy y que la
ÚLTIMA entrada del XFF la escribió él (nginx `$proxy_add_x_forwarded_for`): con un CDN
delante de nginx esa entrada pasa a ser la IP del CDN y el limiter agrupa a todos en un
bucket único — habría que setear ahí la IP del CDN y tomar otra posición del header.

**Al testear la web**: `tests/conftest.py` tiene una fixture autouse `_auth_bypass` que corre los
tests como admin (overridea `get_current_user*` + parchea `templates._get_user_from_token`). Un
test que ejerza la auth REAL marca `@pytest.mark.noauth` (ver `tests/test_auth.py`). El primer
admin lo crea `scripts/init_admin.py` con `MONITOR_ADMIN_PASSWORD` (ya no un default hardcodeado).

## Despliegue

El deploy real es **DigitalOcean** (droplet Ubuntu + nginx + systemd `monitores.service`), vía
`deploy.sh` (`git pull` + `pip install` + `systemctl restart` + healthcheck). Es el ÚNICO
target: Render, Vercel y el `Dockerfile` se dieron de baja (2026-08-31) — no los usaba nadie.
El droplet corre con **systemd + venv** (el venv lo crea `deploy.sh` —lo crea de verdad: valida que
el intérprete sea 3.12 y aborta si no, porque `run.py` exige 3.12.x y un venv de otra minor instala
todo bien y recién revienta en el healthcheck—; está gitignoreado, por eso allá los scripts se
invocan con `venv/bin/python`, no con `python3`). `deploy.sh` corre `pip install -r
requirements.txt` (NO el `.lock`: el lock es para el bootstrap local reproducible).

En prod hay que setear:

- `MONITOR_JWT_SECRET_KEY` — si no, se genera y persiste en `db_dir/jwt_secret` (0600).
- `MONITOR_DB_DIR` fuera del working tree (p. ej. `/var/lib/monitor`). **Alcanza con esa sola
  variable**: desde 2026-09 `db_dir` reubica TODO el conjunto (`catalog.db`, `backups/`,
  `history/`, y los 4 stores `price_history`/`fci_history`/`ratings_history`/`index_history`),
  porque los paths derivados se resuelven en `model_post_init` y no en el cuerpo de la clase.
  Antes era una perilla muerta y había que enumerar 7 env vars por campo — los overrides por
  campo (`MONITOR_CATALOG_DB`, `MONITOR_BACKUP_DIR`, …) siguen existiendo y **ganan**, pero ya no
  son obligatorios, y agregar un store nuevo no obliga a tocar la receta de deploy.
  El default en Linux dejó de ser `<repo>/monitor` (era la trampa: la base viva adentro del árbol
  donde corre `git pull`) y pasa a `$XDG_DATA_HOME/monitor` o `~/.local/share/monitor`, con UNA
  excepción deliberada: si ya existe `<repo>/monitor/catalog.db` se lo respeta, porque mudarlo en
  silencio arrancaría con el catálogo vacío y dejaría la base viva huérfana. Ese caso lo denuncia
  `Settings._check_db_paths` con un ERROR por arranque (`MONITOR_DB_IN_TREE_FATAL=1` para que sea
  fatal en vez de log) — hay que mover las bases y setear `MONITOR_DB_DIR`.
- **TLS**: hoy el droplet sirve por HTTP (nginx catch-all sin `server_name`; Let's Encrypt no
  emite para IP desnuda), así que `cookie_secure` queda en `False` **a propósito**: activarlo sin
  HTTPS hace que el browser descarte la cookie de sesión → login en loop. Cuando haya dominio con
  A a la IP: `bash deploy/setup-https.sh <dominio> <email>` (como root) y recién entonces
  `MONITOR_COOKIE_SECURE=true`. **NO** hace falta agregarle `--proxy-headers
  --forwarded-allow-ips` a uvicorn: 0.48 ya resuelve `proxy_headers=True` y
  `forwarded_allow_ips="127.0.0.1"` por default (verificado con `inspect.signature`
  sobre `uvicorn.Config`), así que `request.url.scheme` sigue el `X-Forwarded-Proto`
  que manda nginx. Esta guía decía lo contrario y mandaba a tocar el `ExecStart` al pedo.
- El **primer admin** no lo crea nadie automáticamente: `deploy.sh` no toca `UserORM`. En un
  droplet nuevo, `venv/bin/python scripts/init_admin.py` con `MONITOR_ADMIN_PASSWORD`.

## Invariantes (no romper)

- **Equivalencia del motor**: `tests/test_pricing_equivalence.py` compara el motor nuevo
  contra el original congelado (`tests/_legacy_engine.py`) sobre todos los instrumentos.
  Cualquier cambio de pricing debe dejarlo verde.
- **`FinancialEngine` preserva firmas** públicas (sus consumidores: bond_detail, generate_report).
- **SQLite (`catalog.db`) = fuente de verdad; Excel/CSV = semillas de bootstrap** (decisión v7.2):
  el catálogo VIVO es SQLite. `CatalogRepository` lee SQLite (auto-siembra del Excel si vacío vía
  `ingest_master.py` / `ingest_from_excel`, que preserva `sheet`+`raw_fields`). Las **ONs** siembran
  de `data/obligaciones_negociables.csv` vía `on_catalog.ingest()`, que es **DESTRUCTIVO**: borra
  TODA la hoja `Obligaciones_Negociables` y la reconstruye del CSV. El guard "solo si la hoja está
  vacía" NO vive adentro de `ingest()` sino en su único caller,
  `apps/web/app.py::_ensure_obligaciones_negociables`. Nunca invocarla a mano sobre una DB poblada:
  se lleva puestas las ON que viven SOLO en la DB (las de bancos del ABM — BACH 30/360,
  BF37/BPCV/BYCV/CACB/CICA — y las de `load_bond.py`/`ingest_irsa_ons.py`), y a diferencia de
  `ingest_master.py` no toma snapshot previo ni chequea `op_guards.server_running`: la pérdida es
  silenciosa y sin backup. Para aplicar cambios del CSV a una DB poblada: ABM o script
  append/upsert (ver `scripts/load_bond.py`). La **ABM escribe SQLite directo** (SQLAlchemy transaccional,
  §5.5 — ya NO toca el Excel) y es el editor de runtime; sus altas viven SOLO en la DB y se ven
  EN CALIENTE (save → `reload()` del repo singleton → el ciclo siguiente del motor las precia;
  sin reiniciar — test `test_abm_save_alta_visible_sin_reiniciar`). `reload()` refresca el cache
  en memoria desde SQLite (NUNCA re-siembra; el camino destructivo vive solo en
  `ingest_from_excel`/`ingest_master.py`). Para cambiar datos ya en la DB: ABM o migración
  explícita, no re-seed. El re-seed (`ingest_master.py`) tiene guards anti-pérdida: aborta con el
  server vivo, si borraría altas DB-only, o si el backup de seguridad pre-reseed falló
  (`--force` para override consciente; el snapshot pre-op es incondicional, `backup_db(tag=...)`).
- **Alta automática de letras = la ÚNICA escritura automática en el catálogo**
  (`apps/web/letras_service.py` + `core/domain/../letras_sync.py`, cableada al final de
  `_price_history_loop`). Trae `GET /v1/finanzas/letras` de ArgentinaDatos y da de alta
  las LECAP/BONCAP que faltan. Como el catálogo es la FUENTE DE VERDAD y esto escribe
  solo desde una fuente de terceros, las reglas son duras y hay tests que las fijan:
  **sólo agrega** (nunca pisa ni borra: no existe camino de update ni de delete, las
  diferencias se reportan por WARNING), **sólo con dato completo** (sin `fechaEmision`
  no hay alta: la API la manda vacía en 12 de 18 y no se puede deducir), **nunca una
  vencida** (la API lista letras muertas: S17A6 seguía en el payload 5 meses después de
  vencer), y **un `tem: 0` es dato AUSENTE, no una tasa de cero** (mismo error que
  `ccp<=0` en `fci_history`). Descarta el payload ENTERO si trae menos del 60% de las
  letras vivas que ya hay (mismo criterio que el corte de ratings). Escribe por
  `instruments_abm.save_instrument` —el borde con los guards— y NO por SQL. Corre
  DESPUÉS del backup del día, así toda alta queda precedida por una copia. Cada alta se
  audita a journald. Perilla: `MONITOR_LETRAS_AUTOSYNC=false` (sigue mirando y avisando,
  sin escribir). A mano: `scripts/sync_letras.py` (dry-run por default, `--apply`).
- **Payoff analítico ⇒ fila ANCLA, nunca schedule**: TAMAR PURO / DUAL / DUAL_CER_TAMAR
  (`instrument_groups.ANALYTIC_PAYOFF_TYPES`, verificada CONTRA el registry por test) cobran
  por fórmula cerrada (`pricing/tamar.tamar_dual_payoff_at`), así que materializarles un
  schedule nominal es un ERROR de datos, no una optimización. En `cashflows` llevan UNA fila
  con `es_ancla=1` (vencimiento, montos 0) que `_orm_to_domain` **filtra**: al motor le siguen
  llegando con `cashflows=()` — el pricing es bit-idéntico POR CONSTRUCCIÓN, no por
  coincidencia numérica — pero el bono queda auditable en la DB y visible en `/cashflows`
  (el router sintetiza el evento de vencimiento desde `maturity_date`, con los montos en
  em-dash porque el importe no se conoce hasta el vto). La regla por tipo vale en las DOS
  puertas de ESCRITURA (`save_instrument` y `save_cashflows` rechazan cargarles flujos) y en
  las TRES de LECTURA: el motor (`_orm_to_domain` filtra el ancla), el form del ABM
  (`get_instrument` la filtra y devuelve `cashflows_source="analitico"`) y el preview
  (`preview_cashflows` no propone nada, porque el save lo descartaría). Backfill de una DB
  ya poblada: `scripts/backfill_tamar_anchor.py` (dry-run por default, forward-only,
  idempotente; imprime contra QUÉ base corre y aborta si no tiene instrumentos — en el
  droplet `MONITOR_DB_DIR` vive en el drop-in de systemd y una shell manual NO lo hereda).
  **Ya corrido en la `catalog.db` local (14 bonos, 2026-09-04).** Ojo con qué compra el
  backfill: `/cashflows` y el pricing son IGUALES con o sin ancla (la fila del panel se
  sintetiza desde `maturity_date`); lo único que cambia es la tabla de completitud del
  ABM, donde `cfn` es un `COUNT(*)` crudo y esos 14 figuran en rojo por «falta
  Cashflows» hasta que se corra.
- **La ABM ya NO sintetiza al guardar**: `cashflow_synth` lee el RELOJ para resolver el step-up
  del cupón, así que el schedule que quedaba en la DB dependía del DÍA DEL ALTA (el mismo form
  daba 0,63% en 2026 y 1,18% en 2028). La síntesis quedó como PREVIEW
  (`POST /abm/preview_cashflows`, botón «⟳ Previsualizar»): el operador la revisa en la tabla
  del cajón —que ahora vive DENTRO del `<form>` de `/abm/save`— y el POST la manda de vuelta.
  Un tipo normal sin flujos se **rechaza** con un mensaje accionable (antes era un WARNING
  silencioso que dejaba un bono impriceable en la DB).
- **Intérprete Python**: usar `py -3.12` / `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`. Ver memoria `env_python_interpreter`. (El viejo "Store Python" ya no existe; sus deps se reinstalaron acá.)
- **Nada de `.db` dentro del proyecto**: las bases viven en `settings.db_dir`
  (`%LOCALAPPDATA%\monitor` en Windows, `$XDG_DATA_HOME/monitor` o `~/.local/share/monitor`
  en Linux; en el droplet, `MONITOR_DB_DIR` en el `EnvironmentFile` del servicio). Motivo: la
  `catalog.db` es la FUENTE DE VERDAD (con las altas del ABM y las cuentas de usuario, que
  viven SOLO ahí, más 4 históricos que se acumulan rueda a rueda y no se backfillean) y no
  debe quedar dentro del árbol sobre el que corre `git pull`/`git clean -xfd`. Esto ya no es
  sólo una convención: `Settings._check_db_paths` **denuncia** por ERROR cualquier base que
  resuelva adentro del working tree, y agregar un store nuevo obliga a colgarlo de
  `_DB_DERIVED` para que `MONITOR_DB_DIR` lo reubique junto con el resto.
  En local se usa `py -3.12` del sistema, sin venv en el proyecto (el venv del servidor lo
  crea `deploy.sh` y está gitignoreado).
- **Schema del catálogo = FORWARD-ONLY**: `init_db` reconcilia con el ORM agregando
  columnas faltantes (`ALTER ADD COLUMN`), **nunca** dropea — un drop borraría las altas
  ABM (que viven solo en la DB). Para transformar datos existentes: migración versionada
  (`CURRENT_SCHEMA_VERSION` + `schema_meta`), jamás recrear. Ver `db/catalog_repository.py`.
- **Un `instrument_type` que no está en `instrument_groups.py` deja el bono INVISIBLE**:
  todo el read-path (paneles, `app._ALL_TYPES`, `on_service`) filtra por **igualdad exacta**
  de tipo, así que la fila se carga, guarda cashflows y acumula precio pero nunca se precia
  ni se muestra. Por eso: (a) agregar un tipo nuevo es **primero** editarlo acá y después
  usarlo; (b) el borde de escritura valida — `repositories._resolve_instrument_type` avisa
  por WARNING cuando el tipo queda huérfano **o** cuando lo asumió del default de una hoja
  ambigua (una ON sin `tipo` cae a hard-dollar y se preciaría en la moneda equivocada), y
  `instruments_abm.save_instrument` rechaza el alta con `is_known_type()`; (c) el estado
  vivo se publica: `CatalogRepository.type_health` → `AppState.set_catalog_health` → bloque
  `catalog` de `/api/health` (cuentas, no tickers: el endpoint es público). Las filas ya
  dañadas se arreglan con `scripts/migrate_orphan_types.py` (dry-run por default).
  Nunca inventar el tipo del nombre de la hoja en un lugar nuevo: ese fue el bug original.
- **`apps/web/static/js/on.js` es AUTO-GENERADO** por `scripts/build_on_static.py` desde
  `apps/web/on_src/` (`sectors.js` + `util.js` + `unified.js` + `on_app.html`). NO editarlo
  a mano: el gate pasa verde, el commit sobrevive, y la próxima corrida del generador borra
  el fix sin conflicto ni rastro. Tocar la fuente en `on_src/` y regenerar.
- **V.Téc / payoff de los DUAL_CER_TAMAR**: la cadena es **settlement T+N → lag CER de 10
  hábiles → spread → max de rieles**, en ese orden. Ya se rompió dos veces (se perdió el lag
  al unificar V.Téc con payoff; se perdió el escalón de liquidación al restaurar el lag).
  El contrato completo, paso por paso, está en el docstring de `core/domain/pricing/tamar.py`
  — leerlo ANTES de tocar `tamar_dual_payoff_at` o `calculate_technical_value`.

## Robustez / Operaciones

- **Backup del catálogo**: snapshot online (consistente con WAL) 1×/día al arrancar,
  rota a `settings.backup_keep` (7) en `%LOCALAPPDATA%\monitor\backups`. Restore:
  `scripts/restore_catalog.py`. Ver `core/infrastructure/db/backup.py`.
- **Verificación TLS por host** (`core/infrastructure/_tls.py`): se **verifica siempre** y
  la allowlist de excepciones arranca **VACÍA**. Hasta 2026-09 exceptuaba `open.` y
  `addin.bymadata.com.ar` por una cadena rota observada en 2026-06; re-verificado EN VIVO el
  2026-09-03 con trust store **certifi-only** (el que usa httpx en el droplet, sin el store
  del SO), los tres hosts BYMA encadenan bien contra GlobalSign RSA OV SSL CA 2018 — la
  excepción quedó obsoleta y mantenerla era degradar dos hosts que ya no lo necesitan.
  Si alguna cadena vuelve a romperse, la perilla es el env `MONITOR_TLS_NO_VERIFY_HOSTS`
  (CSV de hosts); NO volver a poner `verify=False` en un cliente, que deja ese override inerte.
- **Supervisión de los loops** (`apps/web/supervisor.py`): los 5 loops del lifespan van
  envueltos en `supervise()`, que los **reinicia** si terminan por lo que sea (excepción,
  retorno o *cancelación espuria*) con backoff 1s→60s, y reporta el motivo por
  `record_error`. Motivo: `asyncio.create_task` es fire-and-forget — el 2026-09-01
  `_refresh_loop` murió mudo a las 12:45 (`except CancelledError: raise`) y la app sirvió
  el mismo snapshot ~22hs, con los otros 5 loops vivos. El lifespan setea `app.state.stopping`
  **antes** de cancelar: así el supervisor distingue el shutdown real de una caída.
  `_startup_reconcile` NO se supervisa (corre 1× y terminar es su contrato).
- **Zona horaria** (`settings.timezone`, default `America/Argentina/Buenos_Aires`):
  `apply_timezone()` corre al importar `config/settings.py` y fija la TZ del proceso
  (`TZ` + `time.tzset()`). El droplet corre en Etc/UTC y la app usa `datetime.now()`/
  `date.today()` naive: sin esto el header mostraba UTC y, peor, entre las 21:00 y las
  24:00 ART el "hoy" del dominio (settlement, cashflows) ya era el día siguiente.
  **No-op en Windows a propósito**: el CRT de MSVC no parsea nombres IANA y cae a UTC
  (adelantaba 3hs la hora local en desarrollo); allá la TZ del SO ya es la correcta.
- **Observabilidad**: si el refresh loop falla, `AppState.record_error` lo registra y el
  header lo muestra (badge `/health/badge` verde/ámbar/rojo) + `/api/health` da
  `status`/`is_stale`/`last_error`. La app sigue sirviendo el último snapshot bueno.
  **Severidad por loop** (`state._CRITICAL_LOOPS`): la caída del loop **refresh** es
  crítica (badge rojo "sin datos" + `status: degraded`, con retención de 300s para que
  no se la coma el ciclo siguiente); la de los laterales (ratings/bei/price_history/
  options) es degradación **parcial** — va a `status()["loop_crashes"]` (24hs, con
  motivo) y a `degraded_loops` (ventana de 300s, sólo nombres, también en `/api/health`),
  sin apagar el semáforo de unos precios que están frescos: el badge la muestra en
  **ámbar** ("loop caído: <nombre>", motivo en el tooltip — el badge está detrás de
  login), nunca en rojo "sin datos". El supervisor reporta la
  caída con el nombre del loop **estructurado** (`record_loop_crash(name, reason)`), no
  parseando el texto del mensaje.
- **Timeouts de httpx**: el centinela de "usar el timeout del cliente" es
  `httpx.USE_CLIENT_DEFAULT`, **NO `None`**. `None` significa `httpx.Timeout(None)`, o sea
  SIN connect/read/write/pool timeout: un request colgado no vuelve nunca y el loop que lo
  espera queda awaiteando para siempre. `ResilientClient` ya usa el centinela correcto
  (`async_http._USE_CLIENT_DEFAULT`); cualquier wrapper nuevo tiene que copiarlo, no
  "simplificar" a `None`. Guarda: `tests/test_aud_A_infra_http_timeouts.py`.
- **Gate de calidad**: `scripts/check.ps1` (ruff + pytest). Correrlo antes de pushear a
  `origin/main` (el deploy del droplet sale de ahí). `requirements.lock` = instalación
  reproducible en local; el droplet instala `requirements.txt`.
- **Fecha fija en tests** (`tests/_clock.py`): los tests de equivalencia/golden usan
  una fecha de referencia fija (no `date.today()`) para no caducar. Override
  `MONITOR_TEST_REF_DATE=YYYY-MM-DD|today`.
- **Secretos**: credenciales BYMA del usuario en `.env` (gitignored). El client OAuth
  del addin (no secreto, sale del `.xll` público) en `settings.byma_client_*`.

## Flujo Superpowers (método de trabajo)

Este repo usa el plugin **Superpowers** (obra). El flujo para features nuevas es:
`brainstorming → spec → writing-plans → plan → TDD/subagent-driven → code-review →
finishing-branch`. Si se usa, los artefactos van a `docs/superpowers/` (`specs/`,
`plans/`), que se crea on-demand. **Las skills se auto-disparan al arrancar Claude Code**
(no en caliente).

- **Prioridad**: las instrucciones de este CLAUDE.md **ganan** sobre las skills. Si una
  skill choca con una convención de acá (financieras de `agents.md`, equivalencia del
  motor, Excel=semilla), manda CLAUDE.md.
- **TDD aplica** a features nuevas, bugfixes y refactors (test rojo → mínimo verde →
  refactor). Encaja con la disciplina ya existente: `test_pricing_equivalence.py` y la
  suite completa son la red. Un test que no falla al revertir el fix que dice cubrir es
  decorativo: probar la mutación antes de darlo por bueno. Excepción: prototipos
  descartables / config (consultar antes).
- **Worktrees**: worktrees **sí**, pero **nunca** `.worktrees/` dentro del
  proyecto (mantiene el árbol limpio). Usar la tool nativa
  `EnterWorktree` del harness, o el path global `~/.config/superpowers/worktrees/`
  (fuera del working tree). Subagentes paralelos: ojo con los conflictos si se
  trabaja in-place sobre el mismo árbol.
- **Intérprete en los planes**: los comandos de test/run deben usar `py -3.12` (ver
  invariante de abajo), no `python`/`pytest` pelado.

## Pendiente (cola, no funcional)

- **Providers sync restantes**: el hot-path (fuente live + floor, cada 5s) corre async por
  `ResilientClient` + `ProviderHub`, y FX/indices/REM ya tienen `async def prefetch(client)`
  cableado en el `_refresh_loop`. Quedan 100% sync **CAFCI** y **argentinadatos**: `httpx.get`
  directo con cache propio por TTL, corriendo dentro del cómputo de pricing (o sea en
  `to_thread`, fuera del event loop). Es **deliberado** — pasarlos a `ResilientClient` es bajo
  valor / alto riesgo. Ojo: en el camino sync **no hay retry** (el único vive en
  `ResilientClient`) — un provider NUEVO va async, no acá. El helper viejo
  `core/infrastructure/_http.py::http_get_json` **ya no existe** (borrado en f442452); si un doc
  o un comentario todavía lo nombra, está viejo.
- Charts/sparklines adicionales (Chart.js) — ya usado en el panel FCI (`static/js/fci.js`); extender a otros paneles. Más cobertura de tests de routers.
- **FCI composición de cartera**: única pieza no disponible (CAFCI ficha gateada / worker de fonditos pago). El panel la omite hasta conseguir fuente. Flujos: reales vía `fci_history` a medida que acumula ruedas; lente 3m/6m/12m se completa cuando el bootstrap de ~400d de CER/A3500 backfillee.
