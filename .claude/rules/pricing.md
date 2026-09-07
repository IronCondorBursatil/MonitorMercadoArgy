---
paths:
  - "core/domain/pricing/**"
  - "core/domain/*.py"
---

# Pricing — reglas que cargan al tocar el motor

Carga sola al LEER un archivo de `core/domain/pricing/**` o `core/domain/*.py` (no al crear
uno nuevo: esas convenciones están en `CLAUDE.md`). Las convenciones financieras completas
(CER NT8/2024, TAMAR, BEI, day-counts, MD BYMA, precisión) están en
`docs/convenciones-financieras.md`; acá va lo que rompe si no se respeta.

## 1. Equivalencia del motor (red principal)

- `tests/test_pricing_equivalence.py` compara el motor vivo contra el congelado
  `tests/_legacy_engine.py` sobre TODOS los instrumentos, tolerancia **1e-7** (`_close`).
  Cualquier cambio de pricing la deja verde; un test que se auto-skipea es un apagón ruidoso
  (`test_legacy_engine_importable` falla si el legacy no importa).
- El legacy comparte **a propósito** `_xirr_from_years`, `pricing.metrics` y `days_30_360`
  con producción (la copia del solver crasheaba por overflow en CUAP). **No copiarle esos
  símbolos**: su cobertura independiente es `test_xirr_solver.py`, `test_golden_referencia.py`
  y `test_cashflow_synth.py`.
- La equivalencia detecta regresiones, no errores de origen: para eso están los **goldens
  externos** (`test_golden_referencia.py` para ONs/DL/LECAP; `test_golden_tx28.py` para CER,
  contra el Informe Diario del Banco Hipotecario del 2026-09-03: TIR a 0,2 bp, paridad exacta),
  con procedencia y fecha fija. Falta TAMAR (`docs/pendiente.md`).

## 2. Float de punta a punta, sin Decimal — tolerancias canónicas

- Equivalencia **1e-7 rel** · golden TIR **1,5 bp** (`abs=1.5e-4`) · solver XIRR
  **|NPV| < 1e-4** (`xirr._XIRR_TOLERANCE`) · forma cerrada / round-trip **≤ 1e-9 rel**.
- El solver es Brent act/365.25 (`xirr.py`), overflow-safe (NPV devuelve ±inf, no levanta).
  No introducir Decimal ni cambiar el day-count del solver.

## 3. Contrato V.Téc / payoff de DUAL_CER_TAMAR (docstring de `pricing/tamar.py`)

Cuatro pasos, en ESTE orden; cada uno mueve la paridad del panel y la cadena ya se rompió
dos veces (se perdió el lag al unificar V.Téc con payoff; se perdió el escalón de
liquidación al restaurar el lag):

1. **Liquidación T+N** — `settlement_byma_date(end, lag=cer_settle_lag)`: el V.Téc se paga
   contra el CER de la liquidación, no de la rueda. Sólo en el camino V.Téc (`to_date ==
   ref_date`); el payoff a vencimiento va con `cer_settle_lag=None`.
2. **Lag CER 10 hábiles** — `cer_reference_date(<paso 1>, instrument.cer_lag)`: en los DOS
   caminos. Sin él el riel CER se sobrestima ~0,9 % con CER a 2 %/mes.
3. **Spread** — `× (1 + cer_spread)^years`, act/365.25 desde emisión (serie TXMJ*).
4. **Max de rieles** — `max(payoff_tamar, payoff_cer)`, también para el V.Téc devengado.

Guardas: `test_fin_Z1_financiero_vtec_settlement.py`, `test_rem_R1_financiero_cer_lag.py`,
`test_aud_B_financiero_dual_cer_tamar.py`. Leer el docstring ANTES de tocar
`tamar_dual_payoff_at` o `calculate_technical_value`. TAMAR: `k = 365/32`, capitalización
mensual 30/360; MD con `m=12` en TAMAR/DUAL.

## 4. Ancla analítica

Los tipos de `instrument_groups.ANALYTIC_PAYOFF_TYPES` (TAMAR PURO / DUAL / DUAL_CER_TAMAR;
`has_closed_form_payoff`) cobran por fórmula cerrada y llegan al motor con `cashflows=()`
(`_orm_to_domain` filtra la fila `es_ancla=1`). Ninguna strategy debe esperarles un
schedule; materializarles uno es un error de datos, no una optimización.

## 5. Registry y day-count

- `registry.strategy_for(inst)` es la tabla predicado→strategy: un tipo nuevo se enchufa ahí
  (y antes en `instrument_groups.py`), no con un `if/elif` en `services.py`.
  `FinancialEngine` (`services.py`) es fachada: preserva firmas públicas.
- El day-count para descontar es el **declarado por bono** (`Instrument.day_count`, parseado
  por `daycount.parse_day_count`; `year_fraction` es la única función que lo aplica). No
  hardcodear 365 ni 360 en una strategy.
- Settlement: TODO cuelga de `core/holiday_engine.py` (offline, `data/feriados_ar.xlsx`);
  jamás `refresh()` en tests. `today()` es el de `clock.py` (`MONITOR_AS_OF` solo tests);
  moneda por sufijo D/C/— desde `currency.py`, única fuente.
- Un `0`/`≤0` que viene de una fuente externa es dato AUSENTE, no un valor.
