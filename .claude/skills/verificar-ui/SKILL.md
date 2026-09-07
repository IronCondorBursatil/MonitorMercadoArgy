---
name: verificar-ui
description: Verifica la app LOGUEADA en un browser headed vía Playwright MCP —home con todos los paneles de PANEL_ORDER y precios, modal de detalle (T+0/T+1, Esc, foco), /fci y /on con sus fetch, una ruta extra opcional— con consola sin errores, screenshots en el scratchpad y tabla de resultados; reemplaza el «se verifica a mano» de tests/test_modal_a11y.py.
argument-hint: "[base_url] [ruta_extra]"
shell: powershell
allowed-tools: mcp__playwright__browser_navigate, mcp__playwright__browser_snapshot, mcp__playwright__browser_click, mcp__playwright__browser_press_key, mcp__playwright__browser_wait_for, mcp__playwright__browser_evaluate, mcp__playwright__browser_take_screenshot, mcp__playwright__browser_console_messages, mcp__playwright__browser_network_requests, mcp__playwright__browser_tabs, mcp__playwright__browser_close, PowerShell(Invoke-RestMethod *)
---

# /verificar-ui [base_url] [ruta_extra] — la app logueada se ve, responde y no tira errores

`tests/test_modal_a11y.py` valida que el helper `A11yModal` esté cableado en el HTML y
dice, literal, que «el comportamiento DOM se verifica a mano». Este skill ES esa
verificación a mano, pero con el **Playwright MCP** (server `playwright`, browser
**headed**: la ventana se ve, el perfil es efímero) y con evidencia: screenshot por
pantalla, consola y red por página, y una tabla al final.

Cubre lo que `/smoke` no puede (sin cookie no pasa de `/login`): el dashboard con sus
los paneles de `PANEL_ORDER` (14 al 2026-09-07) y precios vivos, el modal de detalle (toggle T+0/T+1, Esc, foco que vuelve),
las dos apps cliente (`/fci` → `/fci/data`, `/on` → `/on/data`) y una ruta más si el cambio
la tocó. **No reemplaza `pytest` ni `/gate`**: complementa.

## Base URL y argumentos

| `base_url` | Qué es | Cómo se levanta / precauciones |
|---|---|---|
| *(sin argumento)* → `http://127.0.0.1:8001` | server de prueba de `scripts/run_server_test.py` | lo arranca `/smoke` en modo «dejar vivo» (ver paso 1); corre con loops → hay precios y SSE |
| `http://127.0.0.1:8000` | el `run.py` productivo local | tiene que estar corriendo; no se arranca ni se mata desde acá |
| `http://129.80.148.166` | producción (OCI, **HTTP plano**, sin TLS) | NUNCA `-SkipCertificateCheck` (está en `deny`); nada de arrancar/matar procesos; el rate-limit del login (5 intentos / 5 min por IP+usuario) es real |

`ruta_extra` (opcional): una ruta tocada por el cambio, se recorre en el paso 7.

## Nombres reales (verificados en `apps/web/app.py`, `routers/*` y `panels_schema.py`)

**Todos los paneles de `PANEL_ORDER`** (14 al 2026-09-07; el número lo da el registro), cada uno un `<tbody id="tbody-{id}" hx-get="/panels/{id}/rows">`:
`bonares`, `cer`, `tasa_fija`, `tamar`, `dolar_linked`, `bopreales`,
`obligaciones_negociables`, `provinciales`, `valor_relativo`, `panel_lider`, `futuros`,
`bei_tenor`, `bei_sendero`, `bei_pares`. El número NO se fija acá: si `panels_schema.py`
cambia, cambia la lista.

| Ruta | Qué es | Detalle que importa |
|---|---|---|
| `/login` | form (h1 «MONITOR AR», botón «Ingresar») | POST OK → 302 a la primera pestaña permitida (normalmente `/`) + cookie httponly `access_token` |
| `/` | dashboard HTMX + SSE (`hx-ext="sse" sse-connect="/stream"`, evento `refresh`) | el modal vive en `<div id="modal">` (base.html); cada ticker es `<a hx-get="/bond/{t}/detail" hx-target="#modal">` |
| `/bond/{t}/detail?lag=0\|1` | fragmento del modal; **`lag=1` (T+1) es el default** | h4: «Descripción», «Métricas (settle …)», «Calculadora», «Cashflows»; botones `T+0`/`T+1` (el activo lleva clase `on`), `✕`; «Proyección CER ▸» sólo en bonos CER |
| `/bond/{t}/metrics` (POST) | calculadora del modal | `hx-target="#calc-result"` |
| `/fci` → `fetch("/fci/data")` | app cliente `static/js/fci.js` | 5 vistas `#vtabs .vtab[data-v]`: `mercado`, `rankings`, `comparar`, `favoritos`, `flujos`; filas `#mbody tr.fund`; carga = «Cargando FCI…», falla = «No se pudo cargar /fci/data.» |
| `/on` → `fetch("/on/data")` | app cliente `static/js/on.js` (autogenerado) | facetas `#fct-sector input[name="sector"][value="<key>"]`; listado `#uni-tool tr.uni-sector[data-sec]`; subtabs `.subtab[data-tab]` = `sectores`/`cards`/`dash`; botón «📊 Ver gráfico» abre el único modal con `role="dialog" aria-modal="true"` (`aria-label="TIR vs MD por sector"`) |
| `/options` | Opciones (la pestaña se llama `opciones`, **la ruta es `/options`**) | chain vacía los primeros ~20 s tras arrancar (loop propio de 60 s) |
| `/curva` `/cartera` `/bcra` `/cashflows` `/escenarios` `/catalogo` `/abm` `/users` | SSR (HTMX) | `/users` sólo admin; candidatas a `ruta_extra` |
| `/api/health` | público | `status`, `instruments`, `is_stale`, `age_seconds`, `degraded_loops` |

## Reglas fijas

- **Credenciales: el skill no las conoce, no las pide, no las tipea.** El login lo hace
  David en la ventana del browser. Por eso `browser_type` y `browser_fill_form` NO están
  en `allowed-tools`, y `.env` está en `deny`: no hay camino para «ayudar» con la clave.
- **Consola con `error` = FALLÓ**, aunque la pantalla se vea perfecta. Sin whitelist: cada
  error va al reporte; si David decide que uno es benigno, queda como «FALLÓ (aceptado:
  motivo)», no desaparece. Los `warning` se reportan, no fallan.
- **Espera.** `browser_wait_for {text|textGone}` corta a los pocos segundos (timeout de
  acción del MCP). Para lo que puede tardar más (el login humano, el primer `/fci/data`,
  un 8001 recién arrancado) usar el **loop**: `browser_wait_for {time: 10}` →
  `browser_evaluate` con la condición → repetir, máximo N vueltas, y reportar si se agota.
- **Snapshots chicos.** El a11y tree de `/` con todos los paneles es enorme: usar `target` para
  acotar (`#modal`, `#tbody-bonares`) o `filename` para mandarlo al scratchpad. Los
  conteos van por `browser_evaluate`, no leyendo el árbol.
- **Screenshots** al scratchpad de la sesión (path absoluto, el que lista el system
  prompt), NUNCA al repo. `scale: "css"` es obligatorio en la tool. Si el MCP fuerza el
  destino a su output dir (`.playwright-mcp/`, gitignoreado), reportar el path real que
  devuelve la tool. Nombres fijos (paso 8).
- **Con el modal abierto los paneles NO refrescan** (`mrRefreshOK` gatea SSE/poll a
  propósito). No es un bug: no esperar cambios de precio con el modal abierto.
- `browser_console_messages` sin `all: true` devuelve lo de la **última navegación**:
  chequear por página, justo antes de irse; el tally global al final con `all: true`.

## Paso a paso

### 1. Precondición: la base responde

```powershell
$base = "http://127.0.0.1:8001"     # o :8000 / http://129.80.148.166
$h = Invoke-RestMethod "$base/api/health" -TimeoutSec 10
"status=$($h.status) instruments=$($h.instruments) is_stale=$($h.is_stale) age=$($h.age_seconds) degraded=$($h.degraded_loops -join ',')"
```

- `instruments > 0` es lo duro. `status` = `ok` idealmente; `degraded` recién arrancado es
  normal hasta el primer refresh (~5-15 s) — re-consultar; si persiste, anotarlo (los
  paneles pueden salir sin precio y el `/on` vacío: es red hacia providers, no UI).
- **Si es el 8001 y no responde**: invocar `/smoke /` en modo «dejar vivo» (su sección
  «Qué NO hace»): en su script, en lugar del `Stop-Process` del `finally`, persistir el
  PID —`Set-Content "<scratchpad>\smoke-8001.pid" $srv.Id`— y **no** matar. El cleanup se
  corre aparte en el paso 9. Sus `allowed-tools` cubren `Start-Process`/`Get-NetTCPConnection`;
  `Stop-Process` está en `ask`: si pregunta, es esperado.
- Si es `:8000` o producción y no responde: parar y decirlo. No se arranca nada.

### 2. Login — lo hace David en la ventana

1. `browser_navigate {url: "<base>/login"}` → `browser_snapshot` debe mostrar el h1
   «MONITOR AR» y el botón «Ingresar». Si en cambio ya cayó en `/` (cookie previa de otra
   corrida en el mismo perfil), saltar al paso 3.
2. Decirle a David, en el chat: «Logueate en la ventana del browser; yo espero.»
3. **Loop de espera** (máx 12 vueltas = ~2 min):
   `browser_wait_for {time: 10}` → `browser_evaluate {function: "() => location.pathname"}`
   hasta que devuelva algo distinto de `/login`. Si se agota: FALLÓ el paso, cerrar y
   reportar (no insistir: cada intento fallido de David cuenta contra el rate-limit).
4. `browser_take_screenshot {scale: "css", filename: "<scratchpad>\01-login-ok.png"}`.
5. Landing esperado: `/`. Si es otra pestaña (usuario sin `bonos`), navegar a `/` y, si
   da 403, anotar que el usuario no tiene la pestaña — no es un fallo de la UI.

### 3. Home: todos los paneles de `PANEL_ORDER`, filas con precio, consola limpia

1. `browser_navigate {url: "<base>/"}` (aunque ya esté: fija el punto de partida de la
   consola y la red).
2. `browser_wait_for {text: "AL30D"}` — el panel BONARES arranca en MEP, así que la fila
   visible es la `D`. Si corta por timeout, loop con
   `() => document.querySelectorAll('#tbody-bonares tr').length`.
3. Conteo de paneles y filas, en UNA evaluación:
   ```js
   () => [...document.querySelectorAll('tbody[id^="tbody-"]')].map(t => [t.id, t.querySelectorAll('tr').length])
   ```
   Deben aparecer los **14 ids** de la lista de arriba. Paneles con 0 filas: `bei_*`
   tardan hasta el primer `_bei_loop` (300 s) y `futuros` depende del WS de Rofex —
   reportarlos como «vacío (esperado)» sólo si `status` de health era `degraded` o el
   server tiene menos de 5 min; si no, FALLÓ.
4. Precio real en la fila conocida (columna 3 = «Precio» en `_SOBERANO_USD_COLS`):
   ```js
   () => { const r = [...document.querySelectorAll('#tbody-bonares tr')].find(r => r.textContent.includes('AL30D')); return r ? r.children[3].textContent.trim() : null; }
   ```
   Tiene que ser un número > 0, no `—` ni `0.00`.
5. `browser_snapshot {filename: "<scratchpad>\02-home.snapshot.md"}` (evidencia, no se lee
   entero) y `browser_take_screenshot {scale: "css", filename: "<scratchpad>\02-home.png"}`.
6. `browser_console_messages {level: "error"}` → **vacío**. Después
   `browser_console_messages {level: "warning"}` → se listan en el reporte.

### 4. Modal de detalle: título, T+0/T+1, Esc, foco

1. Ref del link: `browser_snapshot {target: "#tbody-bonares"}` → tomar el `ref` del link
   «AL30D» → `browser_click {target: "<ref>", element: "ticker AL30D"}`.
2. `browser_wait_for {text: "Calculadora"}` (h4 del fragmento; llega con el swap de
   `/bond/AL30D/detail`).
3. `browser_snapshot {target: "#modal"}` y verificar que aparecen: **`AL30D`** en negrita
   en el head, las secciones «Descripción», «Métricas (settle», «Calculadora»,
   «Cashflows», los botones `T+0`, `T+1` y `✕`. (El fragmento de bono NO lleva
   `role="dialog"`/`aria-modal`: en el árbol es un bloque genérico; su a11y es conductual
   y se prueba en los puntos 4, 6 y 7. Si alguien se los agrega, el snapshot lo mostrará
   como `dialog` — anotarlo como observación, no como fallo.)
4. Estado inicial + foco adentro (`A11yModal.open` enfoca el primer focusable):
   ```js
   () => ({ activo: document.querySelector('#modal .toggle button.on')?.textContent.trim(), focoAdentro: document.getElementById('modal').contains(document.activeElement) })
   ```
   Esperado `{activo: "T+1", focoAdentro: true}`.
5. Toggle: en el snapshot de `#modal` tomar el ref del botón `T+0` →
   `browser_click {target: "<ref>", element: "botón T+0"}` → loop corto hasta que
   `() => document.querySelector('#modal .toggle button.on')?.textContent.trim()` dé `"T+0"`
   → `browser_network_requests {static: false, filter: "/bond/AL30D/detail"}` debe listar
   el `?lag=0` con `[200]`. Screenshot `03-modal-t0.png`.
   (Opcional, si el cambio tocó el focus-trap: `browser_press_key {key: "Tab"}` ×3 y
   `focoAdentro` sigue `true`.)
6. Cierre por teclado: `browser_press_key {key: "Escape"}` →
   `browser_wait_for {textGone: "Calculadora"}`.
7. Modal vacío y **foco restaurado** al link que lo abrió (`prevFocus` del helper):
   ```js
   () => ({ vacio: document.getElementById('modal').children.length === 0, foco: (document.activeElement?.tagName || '') + ':' + (document.activeElement?.textContent || '').trim() })
   ```
   Esperado `{vacio: true, foco: "A:AL30D"}`. Un `foco: "BODY:"` = el helper no restauró:
   FALLÓ.
8. `browser_console_messages {level: "error"}` → vacío.

### 5. `/fci`: fetch OK, filas, cambio de vista

1. `browser_navigate {url: "<base>/fci"}`.
2. `browser_wait_for {textGone: "Cargando FCI…"}`; si corta (el primer `/fci/data` arma
   el dataset CAFCI y puede tardar decenas de segundos; después queda memoizado), loop con
   `() => document.querySelectorAll('#mbody tr.fund').length` hasta > 0 (máx 6 vueltas).
   Si aparece «No se pudo cargar /fci/data.» → FALLÓ, mirar la red.
3. `browser_network_requests {static: false, filter: "/fci/data"}` → `[200]`.
4. Vistas y filas en una evaluación:
   ```js
   () => ({ vistas: [...document.querySelectorAll('#vtabs .vtab')].map(b => b.dataset.v), filas: document.querySelectorAll('#mbody tr.fund').length })
   ```
   Esperado `vistas` = `["mercado","rankings","comparar","favoritos","flujos"]`, `filas > 0`.
5. Screenshot `04-fci-mercado.png`.
6. Cambio de vista: `browser_click {target: "#vtabs .vtab[data-v='rankings']", element: "pestaña Rankings"}`
   → `browser_wait_for {text: "Ranking ·"}` (título del panel de esa vista). Screenshot
   `05-fci-rankings.png`.
7. `browser_console_messages {level: "error"}` → vacío (Chart.js incluido).

### 6. `/on`: fetch OK, filtro de sector, modal con `aria-modal`

1. `browser_navigate {url: "<base>/on"}`.
2. Loop hasta que `() => document.querySelectorAll('#uni-tool tr.uni-sector').length` sea
   > 0 (el listado se pinta con la respuesta de `/on/data`). Si queda en 0 con el fetch en
   200, el panel ON no tiene precios (`PRICE_REQUIRED_PANELS`): cruzar con el `status` de
   health antes de darlo por fallo de UI.
3. `browser_network_requests {static: false, filter: "/on/data"}` → `[200]`.
4. Filtro de sector (la faceta se arma de `ON.SECTORS`; `Telecomunicaciones` no tiene
   espacios ni barras, por eso ese):
   - antes: `n0 = () => document.querySelectorAll('#uni-tool tr.uni-sector').length`
   - `browser_click {target: "#fct-sector input[name='sector'][value='Telecomunicaciones']", element: "checkbox sector Telecomunicaciones"}`
   - después: `() => ({ n: document.querySelectorAll('#uni-tool tr.uni-sector').length, quedo: !!document.querySelector('#uni-tool tr.uni-sector[data-sec="Telecomunicaciones"]') })`
     → `n == n0 - 1` y `quedo: false` (si el sector no tenía bonos en el corte, `n == n0`
     y `quedo: false` también vale — anotarlo).
   - volver a clickear el mismo checkbox para restaurar. Screenshot `06-on-filtro.png`.
5. El modal con `aria-modal` real (es el que cubre `test_on_chart_modal_wired_to_helper`):
   `browser_snapshot` → ref del botón «📊 Ver gráfico» → `browser_click` →
   `browser_snapshot` debe mostrar un nodo **`dialog "TIR vs MD por sector"`** →
   `browser_press_key {key: "Escape"}` → el `dialog` desaparece del snapshot y
   `() => document.activeElement?.textContent.trim()` vuelve a «Ver gráfico».
   Screenshot `07-on-sectores.png` (con el modal cerrado).
6. `browser_console_messages {level: "error"}` → vacío.

### 7. Ruta extra (si vino `ruta_extra`)

`browser_navigate {url: "<base><ruta_extra>"}` → `browser_snapshot` (que no sea el 403 de
«pestaña no habilitada» ni el `/login`) → `browser_wait_for` con un texto propio de esa
página → `browser_network_requests {static: false}` sin 4xx/5xx de la app →
`browser_console_messages {level: "error"}` vacío → screenshot `08-extra.png`. Si la ruta
tiene un fragmento HTMX (`hx-get`), disparar UNA interacción y verificar que el swap llegó
con 200. Para `/options`, esperar a que la chain tenga filas antes de juzgar.

### 8. Screenshots (nombres fijos, todos en `<scratchpad>`)

| Archivo | Momento |
|---|---|
| `01-login-ok.png` | ya fuera de `/login` |
| `02-home.png` (+ `02-home.snapshot.md`) | dashboard con filas |
| `03-modal-t0.png` | modal AL30D con T+0 activo |
| `04-fci-mercado.png` · `05-fci-rankings.png` | FCI, dos vistas |
| `06-on-filtro.png` · `07-on-sectores.png` | ON con un sector destildado · con el modal cerrado |
| `08-extra.png` | `ruta_extra` |

Siempre `scale: "css"`; `fullPage: true` sólo si el fallo está abajo del fold.

### 9. Cierre

1. `browser_console_messages {level: "error", all: true}` → tally global para el reporte.
2. `browser_tabs {action: "list"}` → si la app abrió pestañas de más (un `target=_blank`
   inesperado), anotarlo. `browser_close`.
3. **Sólo si el 8001 lo arrancó `/smoke` en modo «dejar vivo»**: correr el bloque
   `finally` de `/smoke` como llamada aparte, con el PID de `<scratchpad>\smoke-8001.pid`
   (`Stop-Process -Id <pid> -Force` + barrer el puerto hasta que quede libre).
   `Stop-Process` está en `ask`: si pregunta, es esperado. Sin esto el listener queda
   huérfano y el próximo `/smoke` arranca con el puerto sucio.
   `:8000` y producción **no se tocan**.

### 10. Reporte

Tabla, una fila por paso, en este formato:

| Paso | Resultado | Evidencia |
|---|---|---|
| 1 health | OK / FALLÓ | `status=… instruments=…` |
| 2 login | OK / FALLÓ | `01-login-ok.png`, landing `/` |
| 3 home | OK / FALLÓ | 14/14 tbody; AL30D precio `…`; consola: 0 error / N warning (listados) |
| 4 modal | OK / FALLÓ | `03-modal-t0.png`; `{activo, focoAdentro}`; tras Esc `{vacio, foco}` |
| 5 /fci | OK / FALLÓ | `/fci/data 200`; filas N; vistas [5]; `04-…`, `05-…` |
| 6 /on | OK / FALLÓ | `/on/data 200`; sectores n0→n; `dialog` visto y cerrado; `06-…`, `07-…` |
| 7 extra | OK / FALLÓ / N/A | `08-extra.png` |
| 9 cierre | OK | browser cerrado; 8001 libre / no aplicaba |

Regla final, repetida porque es la que se olvida: **consola con `error` = FALLÓ aunque la
pantalla se vea bien**. Cada mensaje de error va textual en la fila del paso donde salió,
con la URL del recurso si era de red. Al pie: `base_url` usada, hora, `status` de health al
empezar y al terminar, y qué quedó sin probar (y por qué).

## Qué NO hace

- **No reemplaza tests.** `pytest`/`/gate` siguen siendo la red; esto es la verificación
  DOM que `test_modal_a11y.py` declara manual. Un fallo acá que un test podría haber
  atrapado es un test que falta.
- **No conoce credenciales** ni las pide por chat ni las tipea: el login es de David en la
  ventana headed. No lee `.env` (deny) ni `jwt_secret`.
- **No corre en CI**: necesita browser headed y una persona en el loop. No prueba con
  `-SkipCertificateCheck` (deny) ni levanta/mata nada que no sea el 8001 de `/smoke`.
- **No prueba el SSE en el tiempo** (que `/stream` empuje `refresh` cada `refresh_sec`).
  Si hace falta, en 3 líneas con `browser_evaluate` sobre `/` logueado:
  instrumentar `() => { window.__sse = []; const es = new EventSource('/stream'); es.addEventListener('refresh', () => window.__sse.push(Date.now())); return 'ok'; }`,
  esperar `browser_wait_for {time: 15}`,
  y leer `() => window.__sse.length` — con `refresh_sec=5` tienen que haber llegado ≥ 2
  (y se puede medir el gap entre timestamps). Cerrar el `EventSource` después.
