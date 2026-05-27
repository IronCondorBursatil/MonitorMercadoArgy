# Soberanos multi-moneda (pricing USD implícito + ABM agrupado) — Diseño

> **Retroactivo.** Feature ya implementada en el WIP de mayo 2026; este spec
> documenta el diseño a posteriori (ver [README](../README.md)).
> Estado: implementada, con tests (4 nuevos en `test_instruments_abm.py`).
> Relacionado: memoria `project_soberano_currency_legs`.

**Goal:** Tratar correctamente un bono soberano como **3 especies por moneda**
(ARS / MEP-D / CABLE-C): pricear bien la pata ARS, filtrar el panel por moneda, y
hacer el ABM agrupado por bono (no por especie suelta).

## Contexto

Un soberano (BONAR ley arg, GLOBAL ley NY) cotiza en 3 monedas: la pata en pesos
(sin sufijo) y las patas MEP (sufijo `D`) y CABLE (sufijo `C`), que ya cotizan en USD.
La pata ARS, si se priceaba con su precio en pesos contra flujos en USD, daba
TIR/MD/paridad sin sentido. Y el ABM editaba especie por especie, sin verlas como un
bono único.

## Arquitectura — 3 frentes

### 1. Pricing de la pata ARS (`core/use_cases/generate_report.py`)
- `_sovereign_ars_usd_price(inst, snapshot, mep_offer, cable_offer)`: para la pata ARS
  de un BONAR/GLOBAL (sin sufijo D/C) devuelve el **precio USD implícito** =
  pesos ÷ offer (MEP si BONAR, CABLE si GLOBAL). `None` si no aplica o falta el dólar.
- Las puntas (`venta`) MEP=`bolsa` y CABLE=`contadoconliqui` se leen **1×** de dolarapi
  al inicio de `execute` (cache class-level), no por instrumento.
- El `pricing_snap` (precio USD implícito) alimenta TIR / V.Téc / MD / paridad; el
  `snapshot` original (pesos) se conserva para el **display** del panel.

### 2. Filtro de moneda en el panel (`apps/web/routers/panels.py`)
- `CCY_FILTER_PANELS = {"bonares"}`: sólo BONARES/GLOBALES tienen las 3 patas limpias.
  Los BOPREAL son casi todos sólo-MEP → el default ARS los ocultaría.
- `_ticker_ccy(ticker)`: sufijo `D`→MEP, `C`→CABLE, resto→ARS.
- En `_build_rows` cada fila lleva `ccy`; el precio de la pata ARS se muestra **sin
  decimales** (91,990). El header del panel ofrece el filtro ARS/MEP/CABLE.

### 3. ABM agrupado por bono (`apps/web/instruments_abm.py` + `routers/abm.py`)
- `_sob_group(ticker)` / `_sob_slot(ticker)`: derivan el bono base y el slot de moneda.
- `get_soberano_form_values(group)`: prefill del form con las 3 monedas del bono.
- `_save_soberano(...)`: alta/edición transaccional de las patas; **sincroniza** —
  una pata vaciada se borra.
- `delete_instrument(key)`: para soberanos borra **todas** las patas del grupo.
- `list_instruments()` consolida las patas en un solo grupo por bono.
- `routers/abm.py`: el form recibe `key` (= grupo para soberanos) y prefilla las 3.

## Data flow
```
dolarapi (bolsa/ccl offer) ─┐
                            ├─> _sovereign_ars_usd_price ─> pricing_snap ─> TIR/MD/paridad
snapshot pesos (display) ───┘                              snapshot pesos ─> panel display
```

## Manejo de errores
- Sin dólar (offer None/≤0) → se usa el precio tal cual (métricas como antes, sin romper).
- Falla por instrumento → `None`, no aborta el batch (invariante ya existente).

## Testing (`tests/test_instruments_abm.py`, +4)
- `test_save_soberano_multi_ticker_creates_legs` — alta crea las patas.
- `test_save_soberano_sync_removes_cleared_leg` — vaciar una pata la borra.
- `test_delete_soberano_group_removes_all_legs` — borrar el grupo borra todo.
- `test_soberano_legs_consolidated_into_one_group` — list consolida en un grupo.

**Gap a cubrir:** falta un test directo de `_sovereign_ars_usd_price` (pesos÷offer,
BONAR vs GLOBAL, retorno None). Agregarlo si se reabre esta feature.

## YAGNI / fuera de alcance
- Filtro de moneda sólo en BONARES (no BOPREAL — necesitarían otro default).
- No se toca la equivalencia del motor (pricing core intacto; el cambio es de input).
