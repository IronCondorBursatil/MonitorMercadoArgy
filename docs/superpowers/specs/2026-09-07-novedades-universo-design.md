# Novedades del universo: refresh diario 8 AM + triage en el ABM

**Fecha**: 2026-09-07 · **Estado**: diseño aprobado (enfoque A) · **Autor**: David + Claude

## Problema

El universo de especies del monitor queda atrás de la realidad del mercado y nadie avisa:

- `byma_catalog` (el «Universo BYMA» del ABM) se siembra de un CSV estático
  (`data/byma/titulos_final.csv`). Una especie licitada después de la fecha del CSV no
  figura ni ahí: el 2026-09-07 la LECAP `S29E7` (licitada en agosto, cotizando en BYMA y
  Data912) no estaba en `instruments` **ni** en `byma_catalog`.
- El reconcile contra Data912 corre solo **al arranque** (`_startup_reconcile`) y los
  tickers de renta fija nuevos «quedan para el alta manual» sin ningún aviso.
- El único mecanismo automático de detección (autosync de letras vía ArgentinaDatos) está
  roto: la API cambió el contrato (devuelve dict y perdió `fechaEmision`). Su fix es un
  bugfix **aparte**; no forma parte de este diseño.

Comparado contra eldashboard.com.ar (2026-09-07) faltaban además: `D30O6`, `D15E7`,
`D31M7`, `D10Y7`, `TZVD8`, `TMVE8`, `PR17` — todas detectables por las fuentes que el
monitor ya consume.

## Decisiones tomadas (con David, 2026-09-07)

1. **Universo vigilado: todo BYMA + todo Data912**, segregado por tipo de activo en la UI
   (cedears, opciones y cauciones incluidos, cada uno en su grupo). David elige qué cargar.
2. **Detección ≠ alta**: el sistema nunca escribe `instruments`. El alta la dispara David
   desde el form prefillado del ABM; los datos financieros finos los carga él.
3. **Aviso: dentro del monitor** (badge global + sección «Novedades» del ABM). Sin email
   ni canales externos.
4. **Ritmo: diario a las 08:00 de America/Argentina/Buenos_Aires**, en el server de prod.

## Fuera de alcance

- Escribir `instruments` automáticamente (el invariante «el alta automática de letras es
  la ÚNICA escritura automática del catálogo» se mantiene: esta feature escribe
  `byma_catalog` y la tabla nueva de novedades, nunca `instruments`).
- El fix del provider de letras de ArgentinaDatos (bugfix separado, con TDD).
- Eliminar los CSV/Excel semilla (va en la fase Excel→DB ya acordada, aparte).
- Estrategias de pricing para tipos nuevos (p. ej. `TMVE8` dual DL/TAMAR): la novedad se
  detecta y muestra; si su tipo no existe en `instrument_groups`, el alta requiere primero
  el trabajo de motor correspondiente (flujo normal de tipo nuevo).

## 1. Datos

**Tabla nueva `universe_novedades`** en `catalog.db`, creada por `init_db`
(forward-only, `ALTER ADD COLUMN` para evoluciones):

| columna | tipo | notas |
| --- | --- | --- |
| `symbol` | TEXT PK | símbolo tal como cotiza (ej. `S29E7`, `BPOA8D`) |
| `first_seen` | TEXT | fecha ISO de detección |
| `source` | TEXT | `byma` / `data912` (la primera fuente que lo trajo) |
| `categoria` | TEXT | snapshot de la categoría al detectar (para segregar en UI) |
| `estado` | TEXT | `nueva` / `cargada` / `descartada` |
| `updated_at` | TEXT | última transición |

Separada de `byma_catalog` a propósito: un re-seed del universo jamás pisa las decisiones
de triage. `byma_catalog` sigue siendo el store de metadata; el job le hace **upsert
(nunca delete)** y gana una columna `last_seen`. El CSV `titulos_final.csv` queda como
semilla de bootstrap (sin cambios acá).

Transiciones de `estado`:

- `nueva` → `cargada`: automática, cuando el símbolo aparece en `instruments` o como pata
  (`ticker_mep`/`ticker_ccl`). La detecta el job y también el save del ABM.
- `nueva` → `descartada`: botón en el ABM. Reversible («Restaurar» → `nueva`).
- Nada se borra nunca; `descartada` es un estado, no un delete.

## 2. Job diario y fuentes

**Loop asyncio nuevo** (`_universe_loop`, mismo patrón que `_ratings_loop`): duerme hasta
las 08:00 de America/Argentina/Buenos_Aires y ejecuta:

1. **Fetch vivo**: la rueda BYMA open (todas las pizarras que la API expone — si hoy el
   provider pide un subconjunto, la corrida amplía la cobertura de LECTURA a las que
   falten: cedears, cauciones, etc.; un tipo de activo que la API no publique no genera
   novedades) + snapshot Data912 (los endpoints que el hub ya consume). Solo las dos APIs;
   ninguna fuente nueva.
2. **Upsert de universo**: símbolos + metadata a `byma_catalog` (solo agregar/actualizar,
   `last_seen` al día; nunca borrar). Enrich de ficha BYMA **solo** para símbolos nuevos.
3. **Diff contra la línea de base** = `byma_catalog` ANTES del upsert de la corrida ∪
   `instruments` ∪ patas ∪ `universe_novedades`. Símbolo visto que no está en la base →
   fila `nueva`. Símbolo en estado `nueva` que ya fue cargado → pasa a `cargada`.
   Consecuencia: la primera corrida NO marca como nuevas las ~4.700 especies del seed CSV
   (ya conocidas: viven en el Universo del ABM); sí aflora los huecos genuinos tipo
   `S29E7` que las fuentes vivas traen y el universo conocido no tenía.
4. **Guard de fuente rota**: si una fuente viene vacía o con forma inesperada, WARNING y
   **no se toca nada** de esa fuente (ni estados ni universo) — mismo espíritu que el
   guard del 60 % del autosync de letras. Un `0`/payload vacío es dato ausente.

Corre todos los días (los findes no habrá novedades; es inocuo). Crash del loop →
`record_loop_crash` → ámbar (loop lateral, no crítico; el semáforo de precios no se toca).
Bajo pytest no arranca (`MONITOR_DISABLE_LOOPS=1`).

**«Refrescar ahora»**: POST admin en el ABM que dispara la misma corrida a demanda
(pasa por `reject_cross_site` como toda mutación).

## 3. ABM renovado

- La pestaña **«Novedades» pasa a ser la primera** del ABM. Grupos colapsables por tipo de
  activo (categoría BYMA / tipo Data912). Por especie: símbolo, denominación, emisor,
  moneda, volumen del día si cotiza (del snapshot del hub, como ya hace el Universo),
  fuente y fecha de detección.
- Acciones por especie: **Cargar** (abre el cajón con el form prefillado) y **Descartar**.
  Las descartadas viven en un grupo colapsado al final con «Restaurar».
- **Prefill enriquecido**: además de lo que ya precarga `prefill_for`, se sugiere
  `instrument_type` por mapeo categoría→tipo (editable; si no hay mapeo claro, sin
  sugerencia — nunca se inventa un tipo: manda el invariante de `instrument_groups`),
  y vencimiento/datos de ficha cuando existan.
- Al guardar un alta cuyo símbolo está en `universe_novedades`, la novedad pasa a
  `cargada` sin intervención.
- La pestaña «Universo BYMA» actual queda como está (buscador general).

## 4. Aviso

- **Badge global** en la barra del monitor con el contador de novedades en estado `nueva`,
  linkeando al ABM. Viaja en el fragment del badge de salud que `base.html` ya refresca
  cada 15 s: cero pollers nuevos.
- `/api/health` expone **solo el contador** (público y recortado: sin tickers), así el
  workflow de staleness lo vigila gratis.

## 5. Manejo de errores

- Fuente rota/vacía → WARNING + skip de esa fuente (punto 2.4). La otra fuente procesa igual.
- Enrich de ficha que falla para un símbolo → la novedad entra igual con la metadata del
  feed; la ficha es best-effort (como hoy en `_startup_reconcile`).
- El botón «Refrescar ahora» reporta el resultado en el ABM (n novedades / error), no en logs.
- El loop nunca puede dejar `universe_novedades` a medias: el diff+upsert de cada corrida
  es una transacción.

## 6. Tests (TDD, fecha fija con `tests/_clock.py`)

- **Diff y transiciones**: fixtures de snapshots → detecta nueva / marca cargada /
  respeta descartada. El test se pone rojo si se revierte la lógica.
- **Guard de fuente rota**: payload vacío o dict inesperado no marca ni borra nada.
- **Upsert solo-agrega** de `byma_catalog`: un símbolo que desaparece del feed no se borra.
- **Rutas**: `TestClient` vía `tests/_routes.py`; las nuevas cuelgan del router ABM
  existente (`RequireTabPermission("abm")`, `app.py:809`); el POST de refresh exige admin.
- **Loop**: no arranca bajo `MONITOR_DISABLE_LOOPS=1`; el cálculo de «próximas 08:00 AR»
  testeado con fecha fija.
- **Health**: el contador aparece en `/api/health` sin tickers.
- `/security-review` antes de pushear (hay POST nuevo).

## Documentación a tocar al cerrar

- `CLAUDE.md`: aclarar que el invariante de escritura automática refiere a `instruments`
  y que `byma_catalog`/`universe_novedades` tienen su propio ciclo diario.
- `docs/flujo-web.md` (loop nuevo) y `docs/arquitectura.md` (tabla nueva).
- `.claude/rules/web.md` no cambia (el ABM no es un panel de `PANEL_ORDER`).
