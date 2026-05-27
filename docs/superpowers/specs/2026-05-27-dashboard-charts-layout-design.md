# Dashboard: gráficos de panel + layout persistente — Diseño

> **Retroactivo.** Feature ya implementada en el WIP de mayo 2026; este spec
> documenta el diseño a posteriori (ver [README](../README.md)).
> Estado: implementada (sin tests de router todavía — ver gap).
> Relacionado: memoria `project_dashboard_panel_toolbar`.

**Goal:** Dar a cada panel un toolbar con (a) popup de **gráfico** TIR×MD con curva
log por grupo, y (b) persistir el **layout** del dashboard (posiciones, paneles
cerrados, columnas) como default del usuario.

## Contexto

Los paneles ya renderizaban tablas SSR desde `AppState`. Faltaba la lectura visual de
la curva (dispersión TIR vs MD) y que el orden/visibilidad de los paneles
sobreviviera al reload. El front ya usa gridstack + localStorage
(`panels-hidden-v1` / `panel-cols-v1`); esto agrega un default server-side opcional.

## Arquitectura — 2 sub-features

### A. Gráfico de panel (popup Chart.js)
- **`templates/fragments/panel_chart.html`** — modal con `<canvas>` + `<script>` que
  htmx ejecuta al swap. Chart.js se carga en el `<head>` de `index.html`.
- **`GET /panels/{panel_id}/chart?ccy=`** (`panels.py`) → `_chart_payload`:
  `[{label, color, points:[{x:MD, y:TIR%, t:ticker}], curve:[{x,y}]}]` por grupo.
  - BONARES separa Bonares (BONAR, azul) vs Globales (GLOBAL, ámbar) en dos curvas;
    el resto va en una sola.
  - Filtra por `ccy` (reusa `_ticker_ccy`); descarta sin MD/TIR.
  - `curve` = ajuste log `TIR = a + b·ln(MD)` (reusa `_fit_log_curve`), 24 puntos.
  - Paneles sin MD/TIR (valor_relativo, panel_lider, futuros, BEI) → `[]`.
- Plugin `tickerLabels`: dibuja el ticker arriba/abajo del punto según esté sobre o
  bajo la curva (interp lineal). Tema dark/light leído de `data-theme`.
- En `index`, cada panel lleva `chartable = bool(types)` para mostrar el botón sólo si
  tiene MD/TIR.

### B. Layout persistente
- **`POST /panels/layout`** — guarda `{layout, hidden, cols}` (JSON) en
  `dashboard_layout.json` **junto a la `.db`** (en `%LOCALAPPDATA%\monitor`, fuera de
  OneDrive). Valida que sea JSON. El usuario lo dispara con "Guardar como default".
- **`DELETE /panels/layout`** — borra el default (vuelve al auto-layout).
- En `index`, `default_layout` se embebe (string JSON crudo o `"null"`); el front lo
  aplica si existe, si no usa el auto-layout + localStorage.

## Data flow
```
AppState.metrics() ─> _chart_payload(panel, ccy) ─> datasets_json ─> Chart.js (popup)
gridstack (front)  ─> POST /panels/layout ─> dashboard_layout.json (junto a .db)
index load         <─ default_layout (embed) <─ _read_default_layout()
```

## Manejo de errores
- `_read_default_layout`: archivo ausente/inválido → `"null"` (auto-layout).
- `POST` con JSON inválido → 400; error de disco → 500 + warning log.
- Popup sin datos → mensaje "Sin datos para graficar (este panel no tiene MD/TIR)".

## Testing — **GAP**
No hay tests de router para los endpoints nuevos. A cubrir si se reabre:
- `_chart_payload`: separación BONAR/GLOBAL, filtro ccy, descarte sin MD/TIR, panel
  no-chartable → `[]`.
- `_read_default_layout`: archivo válido / inexistente / corrupto.
- `POST /panels/layout` con JSON inválido → 400.

## YAGNI / fuera de alcance
- Un solo layout default (no múltiples perfiles).
- Charts sólo en paneles con MD/TIR (no series temporales / sparklines — cola).
- Persistencia en archivo plano, no en la `.db` (es preferencia de UI, no catálogo).
