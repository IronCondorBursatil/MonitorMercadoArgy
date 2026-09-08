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
(nunca delete)** y gana tres columnas: `last_seen` (última corrida que vio el símbolo),
`denominacion` y `vencimiento` (de la ficha técnica BYMA, cuando la trae). El CSV
`titulos_final.csv` queda como semilla de bootstrap, pero **el arranque deja de
re-sembrarla**: hoy `_startup_reconcile` hace DELETE+INSERT del CSV en cada boot y eso
borraría lo que el job agregó; pasa a sembrar **sólo si la tabla está vacía** (mismo modelo
que las ON) y la siembra corre en el **lifespan, antes de crear cualquier loop** (dentro de
`_startup_reconcile` era el 6º paso, después de minutos de fichas, en paralelo con el primer
diff del job). Con estado del job en la tabla (`last_seen`), `ingest_byma_catalog` se
rechaza salvo `force=True`. La corrida del día y cuántos símbolos vio se sellan en `schema_meta`
(`universe_ultima_corrida`, `universe_ultimos_vistos`), como ya hacen los scripts de
backfill con sus propias claves.

Transiciones de `estado`:

- `nueva` → `cargada`: automática, cuando el símbolo aparece en `instruments` o como pata
  (`ticker_mep`/`ticker_ccl`). La detecta el job y también el save del ABM.
- `nueva` → `descartada`: botón en el ABM. Reversible («Restaurar» → `nueva`).
- Nada se borra nunca; `descartada` es un estado, no un delete.

## 2. Job diario y fuentes

**Loop asyncio nuevo** (`_universe_loop`, mismo patrón que `_ratings_loop`): a partir de
las 08:00 de America/Argentina/Buenos_Aires, una vez por día, ejecuta:

1. **Lectura del hub, no fetch nuevo.** A las 08:00 BYMA responde `data: []` (pre-market):
   un fetch fresco rechazaría la corrida todas las mañanas. El hub es stale-safe y conserva
   la rueda anterior (`hub.snapshot()` ∪ `hub.sources()`), que es exactamente «el universo
   de esta mañana». Procedencia: `hub.freshness()` lista lo que trajo la fuente ACTIVA, sea
   cual sea; cuenta como `byma` sólo si `hub.active_mode` empieza con `byma` (con Data912
   activa todo es `data912`); el resto vino del floor Data912. Cobertura = las 6 pizarras BYMA open que el
   provider ya pide (líderes, general, **cedears**, títulos públicos, letras, ON) + los 4
   endpoints Data912. Opciones y cauciones quedan fuera a propósito: son contratos, no
   especies que se carguen en el ABM. Las filas de la rueda no traen metadata (denominación,
   emisor, ISIN, vencimiento): eso sale de la **ficha técnica BYMA, sólo para los símbolos
   nuevos** (best-effort, cap por corrida), y la categoría del bucket del hub
   (`notes`→Letras, `bonds`→Títulos Públicos, `corp`→ON, `stocks`→Acciones,
   `cedears`→Cedears) con la moneda por sufijo (`core/domain/currency.py`).
2. **Upsert de universo**: símbolos nuevos + metadata a `byma_catalog` (solo agregar;
   `last_seen` al día para todo lo visto; nunca borrar ni pisar metadata existente).
3. **Diff contra la línea de base** = `byma_catalog` ANTES del upsert de la corrida ∪
   `instruments` ∪ patas ∪ `universe_novedades`. Símbolo visto que no está en la base →
   fila `nueva`. Símbolo en estado `nueva` que ya fue cargado → pasa a `cargada`.
   Consecuencia: la primera corrida NO marca como nuevas las ~4.700 especies del seed CSV
   (ya conocidas: viven en el Universo del ABM); sí aflora los huecos genuinos tipo
   `S29E7` que las fuentes vivas traen y el universo conocido no tenía.
4. **Guard de lectura rota**: si el hub trae 0 símbolos, menos de 50, o menos del 60 % de
   los que vio la corrida anterior (server reiniciado antes de la rueda, breaker abierto),
   WARNING y **no se toca nada** — mismo espíritu que el guard del autosync de letras. La
   corrida NO se sella como hecha y se reintenta cada hora hasta que entre. Mismo rechazo
   si `byma_catalog` está vacío (sin línea de base: la siembra no corrió o falló) y si ya
   hay una corrida en curso (un lock: el loop y «Refrescar ahora» no se solapan).

Ritmo: primera oportunidad a las 08:00 AR (antes de esa hora duerme hasta las 08:00; si el
día ya se selló duerme hasta las 08:00 de mañana). Corre todos los días (los findes no
habrá novedades; es inocuo). Crash del loop → `record_loop_crash` → ámbar (loop lateral,
no crítico; el semáforo de precios no se toca). Bajo pytest no arranca
(`MONITOR_DISABLE_LOOPS=1`).

**«Refrescar ahora»**: POST admin en el ABM que dispara la misma corrida a demanda
(pasa por `reject_cross_site` como toda mutación).

## 3. ABM renovado

- La pestaña **«Novedades» pasa a ser la primera** del ABM. Grupos colapsables por tipo de
  activo (categoría BYMA / tipo Data912). Por especie: símbolo, denominación, emisor,
  moneda, volumen del día si cotiza (del snapshot del hub, como ya hace el Universo),
  fuente y fecha de detección.
- Acciones por especie: **Cargar** (abre el cajón con el form prefillado) y **Descartar**.
  Las descartadas viven en un grupo colapsado al final con «Restaurar». «Cargar» sólo
  aparece en categorías que tienen hoja en el ABM (Títulos Públicos, ON); Acciones,
  Cedears, Índices y demás sólo ofrecen Descartar (las acciones ya se registran solas al
  arranque; el resto no se precia).
- **Prefill enriquecido**: además de lo que ya precarga `prefill_for`, un **Título Público**
  (categoría BYMA `Títulos Públicos`, sea del panel Letras o Títulos Públicos) cuyo ticker es
  `S`+dígito abre la hoja Tasa Fija con `clase=LECAP`, y `T`+dígito con `clase=BONCAP`
  (TO26/TY30P/TTM26 —letra después de la T— NO reciben clase; una ON con ticker T+dígito como
  T641O tampoco: su categoría es ON). El vencimiento de la ficha prefillea
  `fecha_pago`/`fecha_vencimiento` según la hoja. Nunca se inventa un tipo: manda el
  invariante de `instrument_groups`.
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

- Fuente rota/vacía → no aporta nada nuevo al ciclo, pero el hub conserva la rueda
  anterior de esa fuente (stale-safe). El job no distingue fuentes: aplica el guard de 2.4
  sobre el TOTAL mergeado (BYMA ∪ floor Data912). Sólo con el server recién arrancado antes
  de la rueda el total puede caer bajo el guard; entonces la corrida entera se rechaza y se
  reintenta cada hora. Si el total supera el guard, se procesa lo que haya.
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

## Corrección tras la primera corrida en prod (2026-09-07, 22:15 AR)

La corrida registró **4155 novedades** de 8747 «vistos». Causa: el hub conserva en el
snapshot el maestro entero de BYMA con precio 0 (esqueleto: AA17, AL02H, AL28… vencidos
hace años) y el job lo tomó como visto, contra el invariante «un 0 no es dato»; además
contó como especies el espejo del segmento bilateral (`.SB`, 495), las patas de plazo
especial (X/Y/Z, 797) y cada pata D/C (1306). Diecisiete variantes quedaron con el ISIN
de un bono cargado y `cotiza=1`: `backfill_legs_from_universe` las habría escrito en
`instruments` en el próximo arranque.

Reglas que quedan (regla 4 de `novedades.py`):

- **Visto = cotizó**: sólo símbolos con precio > 0 en el snapshot (`universe_service._cotizo`).
- **Variantes** (`novedades.es_variante`): `.SB` y, en títulos públicos/ON, un símbolo de
  5 letras terminado en X/Y/Z cuya raíz de 4 letras comparte OTRO símbolo visto. Ni entran
  a `byma_catalog` ni son novedad.
- **Una novedad por especie** (`ticker_pesos`), con la pata pesos primero; las patas D/C
  de una especie cargada o ya registrada no son otra novedad (`simbolos_registrados`
  devuelve también la especie de cada registrada).
- **Migración v3** (`catalog_repository`): deshace la corrida defectuosa —borra de
  `byma_catalog` las filas con `last_seen` ajenas al seed, `last_seen` a NULL, borra las
  novedades `nueva` y las claves `universe_*`— para que el job vuelva a correr limpio.
- ABM: «✕ descartar grupo» por categoría (reversible una por una).

### Reglas de David (2026-09-07, misma noche)

- **Unificar por ISIN**: es el mismo activo; el ticker sólo cambia por moneda (D/C) y por
  ámbito de negociación (X/Y/Z). En el job: con la ficha, una novedad por ISIN
  (`universe_service._unificar_por_isin`); con el ISIN de un bono ya cargado o de una
  novedad ya registrada no hay nada que decidir; una pendiente cuya ficha tardía trae el
  ISIN de un bono cargado pasa sola a `cargada`. Sin ficha rige `ticker_pesos`. En
  `instruments`, las patas se unifican por ISIN como siempre (`backfill_legs_from_universe`).
- **Acciones y CEDEARs de alta automática** (`CATEGORIAS_AUTO_ALTA`): es el mismo
  instrumento con sólo el ticker y no necesita datos aparte para sus métricas, así que el
  job los da de alta (`register_stocks`, tipo `ACCION`/`CEDEAR`, sin términos ni flujos) y
  no los lista como novedad; si el alta falla, sí quedan como novedad. `CEDEAR` entra al
  grupo `ACCIONES` de `instrument_groups`.

Ajuste tras la segunda corrida (misma noche): en renta fija **todo** sufijo X/Y/Z es pata
de ámbito (el día en que sólo cotiza la pata X la raíz no tiene hermano: B2N6X, SE7X
quedaron con el ISIN de bonos cargados); en acciones/CEDEARs sigue exigiéndose hermano
de raíz, porque NFLX/SPCX/SKHY/SIEGY son tickers reales. Migración v4: quita esas patas
(GO/CORP con `last_seen`) y sus novedades.
