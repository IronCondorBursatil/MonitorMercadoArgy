# Header strip (dólares + reservas + riesgo país) — Diseño

> **Retroactivo.** Feature ya implementada en el WIP de mayo 2026; este spec
> documenta el diseño a posteriori (ver [README](../README.md)).
> Estado: implementada, con tests. Pendiente: commitear en su rama (ver plan de ramas).

**Goal:** Una franja de cards en el header (visible en todas las páginas) con todos
los tipos de dólar de dolarapi + reservas brutas del BCRA + riesgo país, cada uno con
su variación diaria.

## Contexto

`base.html` envuelve todas las páginas; el header necesitaba un resumen macro de un
vistazo. Los datos ya existían en providers (FX, indices, argentinadatos) pero no
había un punto que los consolidara para el header.

## Arquitectura

Endpoint SSR + fragmento HTMX, igual que los paneles:

- **`apps/web/routers/header.py`** — `GET /header/cards`. Endpoint **sync** (`def`):
  FastAPI lo corre en su threadpool, fuera del event loop, como el resto de read-paths
  sync. Arma el contexto y renderiza el fragmento.
- **`templates/fragments/header_cards.html`** — render de las cards (dólar + reservas +
  riesgo país). Incluido en `base.html` con auto-refresh por polling (`every 60s`):
  los valores cambian lento y los providers cachean con TTL, así que un poll sin dato
  nuevo sólo re-renderiza desde cache.

## Componentes

### `_dolar_cards(fx, prev_close) -> List[dict]`
Una card `{label, compra, venta, var_pct}` por casa de dolarapi.
- Orden curado (`_DOLAR_ORDER`: oficiales primero, luego paralelos/financieros);
  casas nuevas no mapeadas se agregan al final con su `nombre` crudo.
- `_DOLAR_LABELS` mapea casa→label corto (bolsa→MEP, contadoconliqui→CCL, ...).
- `var_pct` = variación de la venta live vs el cierre del día hábil anterior
  (`prev_close[casa]`). `None` si no hay cierre previo.
- Casas sin `venta` se descartan.

### `ArgentinaDatosProvider.get_dolares_prev_close() -> Dict[str, float]`
`{casa: venta del último día hábil anterior a hoy}`. Pega a
`/v1/cotizaciones/dolares` (serie completa), se queda con la fecha máxima `< hoy` por
casa. **Cache por día** (el cierre previo es fijo dentro de la jornada). Nombra las
casas igual que dolarapi → mapeo directo.

### Reservas / riesgo país
`indices.get_reservas_brutas()` + `get_reservas_delta()` (BCRA) y
`arg.get_riesgo_pais()` (EMBI+). Convención de color: reservas suben = verde;
**riesgo país sube = rojo** (malo).

## Manejo de errores
Cada fuente envuelta en try/except → falla silenciosa a vacío/None. El fragmento
renderiza `—` ante datos faltantes sin romper. Una casa caída no tira el header.

## Testing (`tests/test_header_cards.py`, 5 tests)
- `_dolar_cards`: orden de casas conocidas + no mapeadas; descarte de casas sin venta.
- Variación vs cierre previo (signo correcto; casa sin live se ignora).
- Falla del provider → `[]`.
- Render del fragmento con contexto completo (puntas bid/ask, chg con signo/color).
- Render con datos faltantes → placeholders `—`.

## YAGNI / fuera de alcance
- No persiste histórico propio de FX (reusa argentinadatos).
- No sparkline en las cards (queda para charts).
