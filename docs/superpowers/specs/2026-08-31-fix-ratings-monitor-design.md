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
        │  guard de sanidad: corte < 60% del último bueno → se DESCARTA entero
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
- `recent_changes(days=7)` — cambios de la ventana, para el join del panel. La
  ventana se mide contra `fix_changes.fecha` (el corte en que LO DETECTAMOS), no
  contra la fecha que declara FIX: esa puede ser anterior al primer corte y el
  badge nunca se vería.
- DB fuera del working tree, mismo patrón que `fci_history.py` (engine propio,
  reconfigurable para tests).

### 3. `core/infrastructure/ratings.py` (modificado)

- `_entries()`: **merge por emisor**, no fallback por fuente. Se parte del CSV y el
  store PISA emisor por emisor. Motivo (verificado contra el sitio): 2 emisores del
  CSV —Agrality y Metalfor— ya NO figuran en el listado de FIX (calificación retirada),
  así que un fallback "store si hay corte, si no CSV" los borraría del panel al primer
  corte. Con merge, el store aporta 125 emisores frescos y el CSV retiene los que FIX
  dejó de publicar. El **matcher no cambia**.
- `AS_OF` constante → `as_of() -> str` dinámico: fecha del último corte del store,
  o el `AS_OF` del CSV si no hay store. `on_service` deja de importar la constante.
- El cache (`lru_cache`) pasa a invalidarse por corte (key = fecha del corte), no
  por proceso.

### 4. `apps/web/app.py` (modificado)

- `_ratings_loop`: al arrancar y luego cada 6h pregunta al store si ya está el
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

## Hallazgos del scouting (verificados en vivo, 2026-08-31)

El sitio es **Yii2 + Kartik GridView**, server-side rendered. No hay API JSON.

- **URL**: `https://www.fixscr.com/calificaciones` (GET).
- **Params**: `CalificacionesWebSearch[paises_id]=230` (Argentina),
  `CalificacionesWebSearch[section_id]=1` (Finanzas Corporativas) / `=2` (Entidades
  Financieras), `per-page=50`, `page=N`.
- **`per-page` topea en 50**: con 100 o mas el sitio responde **HTTP 500**.
- **Encoding: UTF-8 real** (PAIS viaja como los bytes C3 8D). Decodificar UTF-8, no latin-1.
- **Fila**: `<tr data-key=...>` con 10 `<td>` en este orden:
  `[0]` entidad, `[1]` fecha ISO (`2026-08-06`), `[2]` pais, `[3]` area,
  `[4]` sector, `[5]` tipo de calificacion, `[6]` corto plazo, `[7]` **largo plazo**,
  `[8]` perspectiva, `[9]` estado.
- **Fin de paginacion**: pasada la ultima pagina el sitio **repite la ultima** (no da
  404 ni vacio). Cortar cuando la primera entidad se repite o vienen menos de 50 filas.
- **Volumen**: 638 filas en **14 requests** (Corporativas 406 en 9 pags, Financieras 232 en 5).
  Barato para 1x/dia.
- **Vocabulario de perspectiva** (normalizar al del CSV): `Perspectiva Estable|Positiva|Negativa`
  se le saca el prefijo `Perspectiva `; `N.C` pasa a `N/A`; `RW Positivo`/`RW Negativo`
  ya coinciden.
- **Vocabulario de estado**: `Confirma`, `Sube`, `Baja`, `Asigna`, `Preliminar`, `Nueva`.
  `Sube`/`Baja` es la accion que declara FIX: sirve para **cross-check** del diff propio,
  no lo reemplaza (el estado describe la ultima accion, no el delta contra NUESTRO corte).
- **Politica de fila por entidad**: hay 142 entidades pero solo **73** tienen fila
  tipo `Emisor`. Se toma la mejor fila por entidad: `Emisor` > `Endeudamiento de Largo
  Plazo`, lo que da **125 emisores** (vs 75 del CSV). Filtrar solo `Emisor` perderia 52.

## Manejo de errores

- **Scrape parcial** (timeout a mitad de paginación, HTML cambiado): si el corte
  trae < 60% de las filas del último corte bueno, se descarta entero y se loguea.
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
  AAA > AA+ > … > D (con notches). Vive en `ratings.py`, junto a `_grade()` que
  ya parsea la letra base, y lo consume `ratings_history` al diffear. Un solo
  lugar, testeado.
