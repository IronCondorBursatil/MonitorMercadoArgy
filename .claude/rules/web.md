---
paths:
  - "apps/web/**"
---

# Web — reglas que cargan al tocar `apps/web/**`

Carga sola al LEER un archivo de `apps/web/**` (no al crear uno nuevo: esas convenciones
están en `CLAUDE.md`). El flujo completo (loops, SSE, FCI/ON) está en `docs/flujo-web.md`;
auth en `docs/auth.md` y `.claude/rules/auth.md`.

## Paneles y refresco

- El registro de paneles es `PANELS`/`PANEL_ORDER` en `apps/web/routers/panels_schema.py`.
  Un panel nuevo se registra ahí; no fijar "14 paneles" en otro lado.
- Cada panel es un `<tbody hx-get="/panels/{id}/rows">` SSR desde `AppState`; el refresco es
  event-driven por SSE (`/stream` pushea `refresh` por ciclo) con fallback `every 60s`. TODO
  trigger periódico va gateado por `[mrRefreshOK(this)]` (pestaña oculta / modal abierto) +
  `tabvisible from:body`. No volver a `every 15s`: duplicaba el push del SSE. Los 15s de
  `base.html` son el badge `/health/badge`, no los paneles.
- El motor corre en `to_thread` desde `_refresh_loop`; los routers leen `AppState`, no
  llaman providers. Bajo pytest `MONITOR_DISABLE_LOOPS=1` (ningún loop arranca).

## `on.js` es AUTO-GENERADO

`apps/web/static/js/on.js` lo produce `scripts/build_on_static.py` desde `apps/web/on_src/`
(`sectors.js` + `util.js` + `unified.js` + `on_app.html`), determinista. Editarlo a mano
está bloqueado por `deny` de permisos y por el hook `guard.py`; el gate igual pasa y la
próxima regeneración borra el fix. Tocar `on_src/` y regenerar.

## FCI y ON: apps cliente + datasets memoizados

- `/fci` → `static/js/fci.js` → `/fci/data` (`fci_service.get_fci_dataset`, memoizado por
  corte/día, GZip). `/on` → `static/js/on.js` → `/on/data` (`on_service`, memoizado por
  revisión del snapshot + día) + `/on/pdf`.
- Composición de cartera FCI NO se sintetiza; flujos sólo reales (`fci_history`). El VCP de
  ArgentinaDatos viene por cada 1.000 cuotapartes y `ccp<=0` es dato ausente.

## Auth y superficie pública

- Las rutas públicas están FIJADAS por `tests/test_aud_G_tests_route_auth.py::_PUBLIC_PATHS`
  (`/login`, `/logout`, `/api/health`) + mount `/static`. No crear endpoints sin auth ni
  endpoints de salud nuevos: `/api/health` es el probe externo y por eso va recortado
  (cuentas y nombres de loops; sin `last_error`, sin tickers).
- Todo router nuevo entra en `app.py` con `RequireTabPermission("<tab>")` en `dependencies=`
  (403 con la lista de pestañas; 302 a `/login` sólo para el no autenticado). `/source/*`
  POST y `/users/*` exigen admin.
- Mutaciones (POST/PUT/PATCH/DELETE) pasan por `security_web.reject_cross_site` (origen
  contra `Host`, sin esquema) y toda respuesta por `SecurityHeadersMiddleware`. Un form
  nuevo no necesita token, sí llegar del mismo host.
- `/security-review` antes de pushear cambios en auth o routers.

## Tests

- `TestClient`; la fixture autouse `_auth_bypass` (`tests/conftest.py`) corre como admin.
  Auth real → `@pytest.mark.noauth` (`tests/test_auth.py`).
- Recorrer rutas SIEMPRE con `tests/_routes.py::iter_app_routes`, nunca `app.routes` a
  secas: desde FastAPI 0.141 `include_router` envuelve en `_IncludedRouter` y las deps del
  include viven en `include_context.dependencies`.
- Los tests node de `fci.js` requieren `node`; si falta, el skip es ROJO
  (`tests/_skip_guard.py`), no silencioso.

## Observabilidad

`AppState.record_error` / `record_loop_crash(name, reason)` (estructurado, no parseo de
texto). Sólo el loop `refresh` es crítico (`state._CRITICAL_LOOPS`): badge rojo "sin datos";
los laterales van a ámbar (`degraded_loops`, `loop_crashes` 24h). No apagar el semáforo de
precios frescos por un loop lateral caído.
