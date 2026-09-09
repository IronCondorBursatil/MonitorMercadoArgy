# Flujo web (HTMX SSR)

Movido desde `CLAUDE.md` en la Fase 4 (agents.md §0.8). Para la capa web, la verdad es
ésta (la descripción de `agents.md` anterior a §0 —http.server + SPA `app.js` + `/api/*`—
es histórica). Las reglas que aplican al tocar `apps/web/**` cargan solas desde
`.claude/rules/web.md`; la autenticación está en `docs/auth.md`.

## Lifespan: 6 loops supervisados + un reconcile

`run.py`→uvicorn→`app.py`. El **lifespan** arranca **6 loops supervisados** (los mismos 6
que envuelve `supervise()`, ver `docs/arquitectura.md › Supervisión`) más
`_startup_reconcile` (corre 1× y termina — por eso NO se supervisa). Bajo pytest
(`MONITOR_DISABLE_LOOPS=1`) no arranca ninguno.

- `_refresh_loop` (`settings.refresh_sec`, 5s): `await hub.refresh_all()` trae la **fuente
  live activa** (`byma_open` por default) async con breaker+pool y le mergea el floor
  Data912; después el motor corre `GenerateMonitorReport.execute` vía `to_thread` leyendo
  el snapshot ya materializado → `AppState`. Es el único loop **crítico**. FX/índices/REM
  tienen `async def prefetch(client)` cableado acá.
- `_options_loop` (`settings.options_refresh_sec`, 60s, + una corrida al arranque): chain
  de opciones — parser + CRR + griegos de ~1000 contratos, 5-20s, repartida entre procesos
  (`settings.options_workers`). Va en loop propio a propósito: adentro del refresh lo
  llevaba a ~25s y espaciaba el push SSE de los bonos. De ahí la asimetría 60s vs 5s que
  se ve en el panel de Opciones.
- `_bei_loop` (`bei_refresh_sec`, 300s): `apps.cli.bei.compute_bei_tables`.
- `_price_history_loop` (`price_history_sec`, 1h): mantiene el store de cierres diarios
  para los rendimientos (priming Data912 `/historical/bonds` + acumulación del feed),
  **acumula el corte diario de ArgentinaDatos en `fci_history`** (flujos del FCI), toma
  el backup periódico del catálogo y, al final y DESPUÉS del backup, **da de alta las
  letras nuevas** (`apps/web/letras_service.py`; reglas en CLAUDE.md).
- `_ratings_loop`: 1 corte por día de FIX SCR (si ya está el de hoy no re-scrapea; tras un
  corte nuevo invalida el cache de `ratings`).
- `_universe_loop` — novedades del universo: 1×/día a partir de las 08:00 AR compara el
  snapshot acumulado del hub contra `byma_catalog` ∪ `instruments` ∪ patas ∪
  `universe_novedades`, registra las especies nuevas (estado `nueva`) y publica el contador
  (`AppState.novedades` → badge del header y `/api/health.novedades`). Nunca escribe
  `instruments`; guard de lectura rota (0 / <50 / <60 % de la corrida anterior / universo
  vacío / corrida en curso) → reintento horario. «Refrescar ahora» (POST admin en el ABM)
  corre la misma función. La siembra del CSV en `byma_catalog` corre en el lifespan ANTES
  de crear las tasks, sólo si la tabla está vacía.

## Paneles SSR + SSE

El registro de paneles es `PANELS`/`PANEL_ORDER` en `apps/web/routers/panels_schema.py`
(el número lo da `PANEL_ORDER`; no fijarlo en ningún doc). Receta completa para agregar
uno: `.claude/rules/web.md › Receta: panel nuevo`.

Cada panel es un `<tbody hx-get="/panels/{id}/rows">` que renderiza un fragmento SSR desde
`AppState`; el auto-refresh es **event-driven por SSE** (`/stream` pushea `refresh` por
ciclo). El fallback por polling es `every 60s` —no 15s: se subió en el hardening de
realtime porque duplicaba el push del SSE— y **todos** los triggers van gateados por
`[mrRefreshOK(this)]` (no refrescar con la pestaña oculta / un modal abierto) más
`tabvisible from:body` para repintar al volver. Los 15s que quedan en `base.html` son el
poll del badge `/health/badge`, no los paneles. El detalle es un modal
(`/bond/{t}/detail` + `/bond/{t}/metrics`).

La portada usa `static/js/dashboard_grid.js` para colisiones y el borrador
Aplicar/Cancelar; las filas y filtros no ajustan la geometría. El estado del navegador
se migra a `monitor-dashboard-state-v1` sin borrar las claves anteriores.
`static/js/dashboard_mobile.js` navega los mismos nodos SSR en pantallas estrechas;
`static/css/dashboard.css` adapta navegación, tablas y modales sólo en la portada.
Las reglas de diseño y pruebas para toda UI están en [ui-ux.md](ui-ux.md).

## Paneles FCI y ON (las dos excepciones al SSR)

Sirven una página que carga una app cliente vanilla y ésta hace `fetch` de su dataset JSON.

- `GET /fci` → `static/js/fci.js` (5 vistas + detalle con Chart.js) → `/fci/data`, que arma
  `fci_service.get_fci_dataset` (memoizado por corte/día, GZip) combinando CAFCI
  enriquecido + AUM ArgentinaDatos + lente A3500/CER + flujos reales de `fci_history`.
- `GET /on` → `static/js/on.js` (**auto-generado** desde `apps/web/on_src/` por
  `scripts/build_on_static.py`; nunca editarlo a mano —invariante en CLAUDE.md, deny de
  permisos y hook—) → `/on/data`, que arma `on_service` desde el snapshot vivo
  clasificando por sector al vuelo (+ `/on/pdf`).

## Defensas de borde

`apps/web/security_web.py`: los métodos que mutan (POST/PUT/PATCH/DELETE) se validan por
origen (`reject_cross_site`: `Origin`/`Referer` contra el `Host`, sin esquema, así el
pasaje a HTTPS no obliga a tocarlo) y toda respuesta lleva headers de seguridad
(`SecurityHeadersMiddleware`). Un formulario nuevo no necesita token CSRF, pero sí tiene
que llegar del mismo host. Las docs de OpenAPI están apagadas por default
(`MONITOR_ENABLE_DOCS=1` solo en desarrollo, nunca en prod).

## Rutas públicas

El conjunto está fijado por `tests/test_aud_G_tests_route_auth.py::_PUBLIC_PATHS` (hoy `/login`,
`/logout`, `/api/health`, `/reset/{token}`, `/forgot` + mount `/static`; la lista viva es la del
test, no ésta). Todo lo demás exige login; no
crear endpoints de salud nuevos (`/api/health` ya es el probe externo). Los tests que
recorren `app.routes` usan `tests/_routes.py` (agnóstico a la forma de `include_router`,
que cambió en FastAPI 0.141).
