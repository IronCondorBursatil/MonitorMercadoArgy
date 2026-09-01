# Monitor diario de calificaciones FIX SCR — diseño

**Fecha:** 2026-08-31 · **Estado:** aprobado (brainstorming con el usuario)

## Objetivo

Que el sistema revise 1 vez por día las calificaciones de los emisores del panel ON
contra el listado público de FIX SCR, guarde el historial, y avise en la misma fila
del emisor cuando hubo un cambio (upgrade / downgrade / cambio de perspectiva),
durante los 7 días siguientes al cambio.

Reemplaza el proceso manual actual (pegar el listado en `data/calificaciones.csv` y
actualizar `AS_OF` a mano), que queda como semilla/bootstrap y fallback.

## Decisiones tomadas (con el usuario)

| Decisión | Elección |
|---|---|
| Universo | FIX Argentina, áreas **Finanzas Corporativas + Entidades Financieras**, tipo **Emisor**. Cierra el hueco de bancos/financieras cautivas del panel. |
| Ejecución | **Loop en el lifespan de `app.py`** (como `_price_history_loop`), 1 corte por día. |
| Persistencia | **Store SQLite propio** (`settings.ratings_history_db`, fuera del working tree). El CSV queda como semilla/fallback (decisión v7.2: SQLite=verdad, CSV=semilla). |
| Aviso en el panel | Badge en la fila del rating, visible **7 días** desde el cambio. |
| Matching | Se **reusa el matcher determinista** de `core/infrastructure/ratings.py`. NO fuzzy (rapidfuzz descartado): un rating equivocado es peor que ninguno. |

## Arquitectura

```
[fixscr.com/calificaciones]  (SSR, paginado, filtros por área)
        │  1×/día, httpx + TLS por host, pausa entre páginas
        ▼
core/infrastructure/fix_ratings.py      fetch_listado() + parse_listado(html) puro
        │  guard de sanidad: corte < ~60% del último bueno → se DESCARTA entero
        ▼
core/infrastructure/ratings_history.py  store SQLite: fix_snapshot + fix_changes
        │  record_corte(): idempotente por día; diffea contra el corte anterior
        ▼
core/infrastructure/ratings.py          _entries(): store si hay corte, si no CSV.
        │                               as_of() dinámico (fecha del último corte).
        │                               rating_for() NO cambia de firma ni matcher.
        ▼
apps/web/on_service.py                  por fila: rating_chg={dir,from,to,fecha} si
        │                               el emisor cambió en los últimos 7 días
        ▼
apps/web/on_src/unified.js → on.js      ▲ (up) / ▼ (down) / ⚑ (solo perspectiva)
                                        tooltip "AA-(arg) ← A+(arg) · 28/08/2026"
```

## Componentes

### 1. `core/infrastructure/fix_ratings.py` (nuevo)

- `parse_listado(html: str) -> list[FixRow]` — **función pura**, testeada con una
  fixture HTML real del sitio guardada en `tests/`. `FixRow`: entidad, área, sector,
  rating LP, perspectiva/watch, fecha del rating.
- `fetch_listado() -> list[FixRow]` — itera la paginación (page-size máximo) para
  las dos áreas, con User-Agent normal, pausa cortés entre requests y el contexto
  TLS por host de `_tls.py`. Corre en `to_thread` (patrón de los providers sync).
- Filtro tipo=Emisor: excluye calificaciones de emisiones/fideicomisos (`AAAsf`).

### 2. `core/infrastructure/ratings_history.py` (nuevo) + `settings.ratings_history_db`

- Tablas:
  - `fix_snapshot(fecha_corte, entidad, area, sector, rating, perspectiva)` — un
    corte por día como máximo.
  - `fix_changes(fecha, entidad, area, rating_from, rating_to, persp_from,
    persp_to, tipo)` — `tipo ∈ {up, down, watch}`. `up`/`down` = cambió el rating
    LP (cualquier notch, por orden de la escala nacional); `watch` = cambió SOLO
    la perspectiva/watch.
- `record_corte(rows, hoy)` — si ya existe corte de `hoy`, no-op (restart-safe).
  Si no, graba y diffea contra el último corte anterior → inserta en `fix_changes`.
  El primer corte de la historia no genera cambios. Una entidad que desaparece del
  listado NO genera cambio (puede ser retiro de calificación o hueco del scrape).
- `latest_entries()` — filas del último corte, en la forma que consume el matcher.
- `recent_changes(days=7)` — cambios de la ventana, para el join del panel.
- DB fuera del working tree, mismo patrón que `fci_history.py` (engine propio,
  reconfigurable para tests).

### 3. `core/infrastructure/ratings.py` (modificado)

- `_entries()`: intenta `ratings_history.latest_entries()`; si el store está vacío
  o no disponible, cae al CSV actual (semilla). El **matcher no cambia**.
- `AS_OF` constante → `as_of() -> str` dinámico: fecha del último corte del store,
  o el `AS_OF` del CSV si no hay store. `on_service` deja de importar la constante.
- El cache (`lru_cache`) pasa a invalidarse por corte (key = fecha del corte), no
  por proceso.

### 4. `apps/web/app.py` (modificado)

- `_ratings_loop`: al arrancar y luego cada ~6h pregunta al store si ya está el
  corte de hoy; si no, `fetch_listado()` vía `to_thread` + `record_corte()`.
  Respetar `MONITOR_DISABLE_LOOPS`. En fallo: log + reintento al tick siguiente;
  el panel sigue con el último corte y su `ratings_as_of` viejo a la vista.

### 5. `apps/web/on_service.py` + `on_src/unified.js` + `static/css/on.css` (modificados)

- Por fila del dataset: `rating_chg = {dir: "up"|"down"|"watch", from, to, fecha}`
  o `None`. El join usa el MISMO matcher (`rating_for` ya devuelve el emisor
  canónico; el cambio se busca por esa entidad).
- JS: badge junto al rating (▲ verde / ▼ rojo / ⚑ neutro) con tooltip
  `"<to> ← <from> · <fecha>"`. Editar `on_src/unified.js` y regenerar el bundle
  con `build_on_static.py` (el test espejo existente vigila la divergencia).

## Manejo de errores

- **Scrape parcial** (timeout a mitad de paginación, HTML cambiado): si el corte
  trae < ~60% de las filas del último corte bueno, se descarta entero y se loguea.
  Peor un día sin corte que falsos cambios masivos.
- **Sitio caído / bloqueo**: el loop loguea y reintenta al tick siguiente. El panel
  nunca se queda sin dato: sirve el último corte, y `ratings_as_of` muestra la
  fecha real de vigencia.
- **Cambio de estructura del HTML**: el parser valida las columnas esperadas y
  falla ruidosamente (log) en vez de devolver filas vacías.

## Testing (TDD, red → verde)

1. `parse_listado` contra fixture HTML real (filas, filtro de emisiones sf).
2. Guard de sanidad del corte parcial.
3. Store: idempotencia por día, diff up/down/watch, primer corte sin cambios,
   entidad desaparecida sin cambio, `recent_changes` respeta la ventana.
4. `ratings._entries()`: store con datos vs fallback CSV; `as_of()` dinámico.
5. `on_service`: fila con cambio reciente expone `rating_chg`; sin cambio, `None`.
6. Espejo `on_src` ↔ `on.js` (test existente).
7. Wiring del loop con `MONITOR_DISABLE_LOOPS`.

## Fuera de alcance (a propósito)

Vista de historial completo, notificaciones push/email, matching por CUIT, ratings
por emisión, otras calificadoras (Moody's Local, S&P). Todo montable después sobre
`fix_changes`.

## Riesgos

- **Fragilidad del scraping**: fixscr.com puede cambiar el HTML o bloquear. Mitiga:
  parser ruidoso + guard de sanidad + CSV como fallback permanente + 1 request-set
  por día (cortés).
- **Orden de la escala nacional**: para clasificar up/down hace falta el orden
  AAA > AA+ > … > D (con notches). Vive en un solo lugar, testeado.
