# Convenciones financieras del Monitor

> **Qué es este archivo.** La ÚNICA fuente de verdad de las convenciones financieras del
> repo: settlement, calendario, day-counts, solver, tasas, MD, accrued, CER, TAMAR,
> dólar-linked, hard-dollar, soberanos por moneda, LECAP, futuros, BEI, precisión, goldens
> y el esquema de campos del instrumento. Vivían en `agents.md › CONVENCIONES CRÍTICAS`;
> se mudaron acá en la Fase 4 (2026-09-07) **corregidas contra el código**: cada regla cita
> `archivo:línea` verificado sobre HEAD `c0dda9d`. Donde el documento viejo decía
> «⚠️ DESACTUALIZADO (el código manda)» acá está SOLO la versión vigente; lo refutado queda
> en la sección final para que nadie lo vuelva a escribir.
>
> **Orden de autoridad**: `CLAUDE.md` › `agents.md §0` › este archivo. Si el código y este
> archivo difieren, el código manda y se corrige ACÁ (no se re-copia en otro lado,
> `agents.md §0.1.4`). Un cambio de convención = cambio de código + línea acá + test guardián.
>
> **Papers de referencia**: BCRA NT N°3/2019 (Corso & Matarrelli) y NT N°8/2024
> (Matarrelli & Pastore). **Títulos citados por tests** en docstrings (no renombrar):
> «Bonos CER (NT N°8/2024)» y «Bonos TAMAR (PURO, DUAL, DUAL_CER_TAMAR)».
>
> Lo marcado **«no verificado en código»** viene del documento viejo o de los papers y no
> tiene test ni línea de código que lo respalde: se conserva como referencia, no como hecho.

## Índice

1. [Reglas transversales](#reglas-transversales) — settlement T+1, calendario offline,
   day-count, ex-cupón, solver XIRR, conversión de tasas, MD BYMA/IAMC, accrued, stub final,
   moneda por sufijo.
2. [Bonos CER (NT N°8/2024)](#bonos-cer-nt-n82024)
3. [Bonos TAMAR (PURO, DUAL, DUAL_CER_TAMAR)](#bonos-tamar-puro-dual-dual_cer_tamar)
4. [Bonos DOLAR LINKED](#bonos-dolar-linked)
5. [Obligaciones Negociables (ON hard-dollar)](#obligaciones-negociables-on-hard-dollar)
6. [Soberanos: 3 especies por moneda (ARS / MEP / CABLE) + pricing de la pata ARS](#soberanos-3-especies-por-moneda-ars--mep--cable--pricing-de-la-pata-ars)
7. [Bonos LECAP / BONCAP capitalizables](#bonos-lecap--boncap-capitalizables)
8. [Futuros DLR, valor relativo y escenarios](#futuros-dlr-valor-relativo-y-escenarios)
9. [Curvas y BEI (NT N°3/2019 + NT N°8/2024)](#curvas-y-bei-nt-n32019--nt-n82024)
10. [Política de precisión y tolerancias](#política-de-precisión-y-tolerancias)
11. [Verificación independiente: inventario de goldens](#verificación-independiente-inventario-de-goldens)
12. [Schema de las hojas / campos del instrumento](#schema-de-las-hojas--campos-del-instrumento)
13. [Cómo extender la matemática financiera](#cómo-extender-la-matemática-financiera)
14. [Refutado — no volver a escribir](#refutado--no-volver-a-escribir)
15. [No verificado en código](#no-verificado-en-código)

---

## Reglas transversales

### Settlement: T+1 para todos

- `conventions.settlement_for(instrument_type)` (`core/domain/conventions.py:104-121`):
  **`lag = 1` para TODOS los tipos**, LECAP/BONCAP/LECER incluidos. El parámetro
  `instrument_type` se conserva en la firma por compatibilidad y no distingue nada. Cache
  diaria (se invalida al cambiar el día calendario).
- `resolve_settle(instrument_type, override)` (`conventions.py:124-127`): el override manda.
  Lo usa la calculadora del popup: `bond_detail._resolve_ref(settlement_lag)`
  (`apps/web/bond_detail.py:351-356`): lag 0 → `today()` del dominio; lag N → `settlement_byma`.
- `settlement_byma_date(trade, lag)` (`core/holiday_engine.py:230-247`) admite SOLO 0 y 1
  (T+2 no existe en BYMA; otro lag → `ValueError`). T+0 = mismo día si es hábil, si no el
  siguiente hábil. T+1 = siguiente día hábil (salta fin de semana y feriados: viernes
  2026-03-20 → miércoles 2026-03-25, `tests/test_holiday_calendar.py:45-49`).
- **V.Téc y el escalón de liquidación**: `FinancialEngine.calculate_technical_value(snapshot,
  indices, fx, ref_date, settle_lag=1)` (`core/domain/services.py:57-65`). `ref_date` es la
  fecha de RUEDA; `settle_lag` viaja en `PricingContext.settle_lag` (`core/domain/pricing/
  context.py:23-25`) y se aplica ADENTRO, al elegir el CER que indexa (ver Bonos CER). El
  popup ya resolvió el settle, así que pasa `settle_lag=0` (`bond_detail.py:383-388`); sin
  eso hay doble lag (+0,26 % de V.Téc en CI, observado).
- La calculadora del popup expone el toggle T+0/T+1 y recalcula TODO (TIR, MD, V.Téc,
  accrued, residual) desde la misma fecha (`bond_detail._live_metrics`, :359-394): sin esto
  la TIR quedaba en T+1 y el V.Téc en T+0 (inconsistencia silenciosa de un día).

### Calendario BYMA y feriados AR (100 % offline en runtime)

- `holiday_engine._ar_holidays()` (`core/holiday_engine.py:121-137`) lee
  `data/feriados_ar.xlsx` (hoja «Feriados AR», trackeado en git) y DESCARTA las filas cuya
  «Fuente principal» es solo `xbue_pmc` (pandas_market_calendars ubica mal puentes y
  trasladables; 2025-11-17 era una fecha espuria). Resultado: frozenset de **197 fechas**,
  cobertura **2020–2029** (`AÑOS_COBERTURA = range(2020, 2030)`, `:67`).
- **Hash del set derivado** (`sha256("\n".join(sorted(iso)))`, recomputado 2026-09-07):
  `56aed962fb13dc47164060044ce7a1dab7deede6f02a0e1c959c12a3d5ad2818`. No hashear los bytes
  del xlsx (un re-save cambia la metadata).
- `es_habil(date)` (`:190-195`, O(1), date-native; preferir sobre `is_habil(str)`): fin de
  semana → no; dentro de la cobertura → «no es feriado del set»; fuera de la cobertura →
  fallback XBUE por año (`:147-171`).
- `refresh()` / `descargar_todos()` (`:846-869`, `:550`) bajan de 5 fuentes por red y
  **REESCRIBEN el xlsx**: jamás llamarlas en tests (`agents.md §0.9`).
- Anclas fijadas por `tests/test_holiday_calendar.py`: 2026-03-23 (puente), 2026-03-24,
  2025-11-21, 2025-11-24 no hábiles; 2025-11-17 hábil; sábado/domingo no hábiles; T+1 del
  2026-03-20 = 2026-03-25; `cer_reference_date(2026-03-31, 10) = 2026-03-13` y
  `cer_reference_date(2025-11-28, 10) = 2025-11-12` (ver cer_base en Bonos CER).
- **Todo el settlement cuelga de acá**: un feriado mal cargado mueve la liquidación de todos
  los bonos y el CER de referencia (lag de 10 hábiles) de cada bono indexado.

### Day-count / convención de descuento

- `daycount.year_fraction(start, end, DayCount)` (`core/domain/daycount.py:117-136`) es la
  ÚNICA fracción de año para DESCONTAR (TIR / MD / PV / convexidad). `DayCount` (`:34-40`):
  `ACT/365` (días/365), `ACT/365.25` (días/365.25 — año juliano, **default soberano**),
  `30/360` (ISDA, `days_30_360/360`), `ACT/ACT` (ISDA actual/actual, `:97-114`: cada año
  calendario con su largo 365/366; un año completo da 1.0 exacto).
- `parse_day_count(raw)` (`:74-90`) tolera alias, mayúsculas, espacios, coma decimal y
  basura; desconocido o vacío → `ACT/365.25`; **nunca lanza**. Orden: «365.25» antes que
  «365», «ACT/ACT» antes que «ACT/365». Memoizada (`lru_cache(128)`).
- `Instrument.day_count_enum` (`core/domain/models.py:197-216`): **BOPREAL fuerza 30/360**
  (espeja `is_30_360`, `:192-194`). `Instrument.year_fraction_to(target, ref)` (`:218-225`) es
  el único punto por el que strategies y metrics descuentan.
- `days_30_360(start, end)` (`core/domain/cashflow_synth.py:122-128`) es la fuente única del
  30/360 (`d1 = min(d1, 30)`; `d2 = 31 → 30` si `d1 ≥ 30`); `daycount` y `conventions` lo
  re-exportan. Fija: 2025-05-30 → 2026-05-30 = 360; 2025-05-30 → 2026-05-29 = 359; 2025-05-30
  → 2025-07-31 = 60 (`tests/test_cashflow_synth.py:23-39`).
- Default al construir el instrumento (`core/infrastructure/repositories.py:392-398`):
  `base calculo` declarado > BOPREAL `30/360` > ON (`HARD DOLLAR` / `DOLLAR LINKED`)
  `ACT/365` > `ACT/365.25`.
- Garantía de equivalencia: para `30/360` y `ACT/365.25` el resultado es bit-idéntico al
  motor viejo; solo cambió el descuento de los `ACT/365` (ONs y dólar-linked). Prueba:
  CICA 7,56 % con ACT/365 vs 7,57 % con 365.25 (`tests/test_golden_referencia.py:231-246`).
- **Dos convenciones distintas conviven en un mismo bono**: la del PAGO (cómo se calcula el
  cupón o el payoff: `cashflow_synth`, `on_cashflows`) y la del DESCUENTO (`year_fraction`).
  Un LECAP paga por 30/360 y se descuenta por la base declarada (default 365.25); un
  BONCER 30/360 deflacta por CER (act/365.25 en la proyección) y descuenta 30/360.
- Tests: `test_daycount.py` (identidades, bordes bisiestos, fin de mes, ACT/ACT),
  `test_xirr_solver.py`, `test_daycount_pricing.py` (descuento por convención + gap-lock
  ACT/365 vs 365.25), `test_golden_referencia.py`, `test_pricing_invariants.py` (hypothesis:
  round-trip, monotonía, cotas). Hueco conocido: `test_daycount.py:107-120` compara
  `year_fraction(30/360)` contra `days_30_360/360` (tautológico) — faltan valores a mano para
  31→30 y 29-feb (`agents.md §0.8` Fase 5).

### Ex-cupón: corte estricto en la fecha de referencia

- Un flujo que paga EXACTAMENTE en la fecha de liquidación lo cobra el VENDEDOR (el
  comprador liquida ese día y no es tenedor de registro): futuros = `date > ref`, pasados =
  `date <= ref` (`core/domain/models.py:227-232`, `core/domain/pricing/metrics.py:40-46`,
  `pricing/base.py:44-45`). Vale para TIR, accrued, residual y V.Téc.

### Solver de TIR (XIRR) — `core/domain/xirr.py`

Tres caminos, en este orden (`_xirr_from_years`, `:151-209`):

1. **Forma cerrada para 2 flujos** (`_closed_form_two_flows`, `:67-119`):
   `r = (pago/precio)^(1/Δt) − 1` cuando hay un solo cambio de signo (`f0 < 0 < f1`) y
   `Δt > 0`. Ventana `[−0.9999, 2^59]` — la misma que alcanza el bracketing — para no
   publicar TIRs que el camino histórico no encontraba. Cubre LECAP / BONCAP / LECER /
   BONCER ZC y cualquier bono al que le quede un solo flujo.
2. **Newton** (`scipy.optimize.newton`, `maxiter=50`) con seeds `(0.05, 0.20, −0.10, 0.80,
   −0.50)` (`:33-34`), más un `seed` opcional de warm-start. Se acepta SOLO si converge
   limpio: finito, `> −1` y `|NPV| < 1e-4` (`_XIRR_TOLERANCE`, `:35`, `:206`).
3. **brentq con auto-bracketing** (`_bracket_and_solve`, `:122-148`): arranca en
   `[−0.9999, 1.0]` y duplica el techo hasta 60 veces (`≈1.15e18`) hasta encontrar cambio de
   signo; `brentq(maxiter=200, xtol=1e-12)`. NPV monótona (sin raíz) → `NaN` limpio, nunca
   excepción. `_npv` es overflow-safe (`:54-64`).

- `xirr(flows, dates, day_count=None)` (`:212-224`): sin `day_count` usa años julianos
  365.25 (back-compat); las strategies ya le pasan `years` descontados con la convención del
  bono (`metrics.discount_year_fractions`).
- TIR ≤ −100 % es degenerada: MD, PV, convexidad y `price_from_tir` devuelven `None`
  (`pricing/base.py:113-117`, `metrics.py:352-353`, `:381`); `(1+tir)^(1/12)` con base ≤ 0 daría
  un complejo, no una excepción, por eso el guard es explícito.
- Refutado: bracket fijo `[−0.999, 10]` (perdía yields > 1000 % y tasas en `(−1, −0.999)`).

### Conversión de tasas (TEA ↔ TEM ↔ TNA) — `core/domain/conventions.py`

| Función | Fórmula | Uso |
|---|---|---|
| `tamar_tem(tna)` (`:21-33`) | `((1 + TNA/k)^k)^(1/12) − 1`, `k = 365/32 ≈ 11.40625` | fórmula oficial BONTE TAMAR |
| `tea_to_tem(tea)` (`:39-46`) | `(1+TEA)^(30/365) − 1` | TEM act/365 (panel Tasa Fija, carry) |
| `tea_to_tem_m12(tea)` (`:84-91`) | `(1+TEA)^(1/12) − 1` | TEM 30/360 Secretaría de Finanzas (`tem_360` del popup) |
| `tea_to_tna(tea)` (`:59-66`) | `365 × ((1+TEA)^(1/365) − 1)` | TNA base 365 (capitalización diaria) |
| `tea_to_tna_monthly(tea)` (`:49-56`) | `12 × ((1+TEA)^(1/12) − 1)` | TNA m=12: «Tir Nominal» IAMC de TAMAR/DUAL y capitalizables peso (LECAP/BONCAP/LECER) |
| `tea_to_tna_freq(tea, m)` (`:69-81`) | `m × ((1+TEA)^(1/m) − 1)`, `m` = frecuencia de pago | «Tir Nominal» IAMC de bonos CON cupón (semestral m=2, trimestral m=4) |

- Todas devuelven `None` si `TEA ≤ −1` u overflow. La TIR interna es SIEMPRE TEA (decimal,
  0.30 = 30 %); las demás son display.
- Selección de la «Tir Nominal» por tipo: `bond_detail._nominal_tna`
  (`apps/web/bond_detail.py:69-84`). El popup publica `tna` (base 365), `tna_mensual` (m=12),
  `tem` (30/365) y `tem_360` (m=12) a la vez (`:405-408`, `:606-610`).

### Modified Duration — convención BYMA/IAMC

- Vanilla (`core/domain/pricing/base.py:110-133`): `MD = Macaulay / (1+TEA)^(1/m)`, con
  `m = payment_frequency` (2 = semestral) y **`m = 1` si queda un solo flujo**. Descuenta con
  `discount_year_fractions` (convención del bono + extensión de stub). TIR ≤ −1 → `None`.
- TAMAR PURO / DUAL y DUAL_CER_TAMAR: bullet con **`m = 12`** (capitalización mensual) →
  `MD = años / (1+TEA)^(1/12)` (`pricing/strategies.py:198-205`, `:302-328`). Guardián:
  `tests/test_rem_R1_financiero_dual_md.py` (la excepción m=1 es Dólar Linked, no este tipo).
- Dólar-linked y hard-dollar heredan la vanilla (`m = freq`; zero-coupon → 1) (`:139`).
- `payment_frequency` cuando la fila no lo trae: `_infer_payment_frequency`
  (`core/infrastructure/repositories.py:21-40`) — gap mediano entre flujos, redondeado a
  12/4/2/1; un solo flujo → 1.

### Intereses corridos (accrued) y período corriente

- `metrics.accrued_interest(inst, ref)` (`core/domain/pricing/metrics.py:115-174`): accrual
  lineal sobre el cupón corriente, per-100 VN. **0** para zero-coupon y capitalizables
  (LECER, LECAP, BONCER ZC, PURO, DUAL).
  - **30/360**: período y transcurrido con `days_30_360`, contados desde la fecha
    PROGRAMADA del cupón anterior, sin correr a día hábil (`:134-144`). PLC4: cupón sábado
    30/05 → 10 días al 10/06, no 9 → 0,2361 (`test_golden_referencia.py:168-172`).
  - **ACT/\***: los días corren desde la fecha de PAGO real = día hábil *following* del corte
    (`_following_business_day`, `:101-112`); tasa diaria = cupón anterior / período PROGRAMADO
    (robusto ante «long last coupon»); primer período → prorrateo del próximo cupón sobre
    los días reales (`:146-174`).
- `period_bounds(inst, ref)` (`:33-63`), el período corriente: (a) hay flujo pasado → el
  último; (b) sin flujo pasado y emisión dentro de 2 períodos del próximo cupón → arranca en
  la EMISIÓN (primer cupón regular, corto o largo — CS50: 9 meses = 1,5 períodos); (c) si no,
  es un soberano mid-amort con flujos pasados recortados (AL29D/AL30D/GD30D) →
  `prev = próximo − 12/freq meses` con `relativedelta` (aritmética calendario exacta).
  Refutado: caer siempre a `emission_date` (AL29D daba 2.082 días en vez de 130).
- `days_since_last_coupon` (`:285-295`) usa el mismo inicio que el accrued (30/360 desde el
  corte programado; ACT desde el pago real). `residual_nominal` (`:298-309`) = Σ amortizaciones
  futuras, fallback `100 − amortizado`. `current_yield` (`:312-345`) = cupón anual
  (`interest / dcf` del período) / precio **clean** — convención IAMC; `None` sin cupón.
  `dv01` (`:368-376`) = `P(tir − 1bp) − P(tir)`, positivo. `convexity` (`:379-401`) en años²
  (`ΔP/P ≈ −MD·Δy + ½·C·Δy²`).
- `calculate_technical_value` (vanilla) = residual + accrued (× factor CER si aplica),
  `pricing/base.py:65-86`.

### Período final stub (extensión ISMA)

- `metrics.discount_year_fractions` (`metrics.py:66-98`): un último período CORTO —
  entre el 10 % y el 90 % del regular (`_STUB_MIN_FRAC/_STUB_MAX_FRAC`, `:19-20`) — cuyo
  penúltimo flujo es un cupón (`interest > 0`) y cuyo cupón final es ~COMPLETO (≥ 90 % del
  regular, `_STUB_FULL_COUPON_FRAC`, `:26`) se descuenta al FIN del período regular. Un
  cupón final PRORRATEADO al stub no se extiende (se devengó en el período corto).
- Goldens: CLISA 2031 extiende (TIR 17,03 %; sin extensión daría 17,47 %) y YM42 no extiende
  (5,60 %; extendido daría 5,16 %) — `tests/test_golden_referencia.py:28-60`, `:99-128`.

### Moneda por sufijo del ticker

- `core/domain/currency.py::ccy_from_suffix`: última letra **D → MEP, C → CABLE, resto →
  ARS**. Única fuente para soberanos, BOPREAL y ONs multi-pata (`agents.md §0.1.14`); ninguna
  capa re-implementa la regla.

---

## Bonos CER (NT N°8/2024)

#### `cer_base` (columna `cer emision` / `cer_emision` / `cer_base`)

- Es el **CER de 10 días hábiles BYMA ANTES de la fecha de emisión**, no el del día de
  emisión. `build_instrument` lo lee de `cer emision` (legacy) / `cer_emision` (snake) /
  `cer_base` (duales TAMAR) (`core/infrastructure/repositories.py:369-370`). **Default 1.0
  si falta**: el bono se pricea SIN deflación (TIR nominal disfrazada de real) — cargarlo
  siempre.
- Lag: `dias habiles previos` / `dias_lag` → `Instrument.cer_lag`, **default 10**
  (`repositories.py:371`, `core/domain/models.py:76`).
- Anclas validadas contra BCRA (variable 30): TZXS7/TZXS8/TZXM8 `cer_base 723.06` = CER del
  2026-03-13 (emisión 2026-03-31 − 10 hábiles); X29Y6/TZXA7 `651.89806` = CER del 2025-11-12
  (`tests/test_holiday_calendar.py:37-42`). TX28 (ISIN ARARGE3209X6, emisión 2020-09-04):
  `cer_base 22.5439510896` = CER del 2020-08-21 (verificado contra la API v4.0 variable 30
  en la auditoría 2026-09-07, `agents.md §0.6`) — candidato a golden BONCER (ver inventario).
- Ejemplo del paper (T2X5: emisión 14/03/2023 → CER del 28/02/2023 = 81.22):
  **no verificado en código** (no está en ningún test ni fixture); ilustra la regla.

#### Cashflows en términos «base»

- La tabla `cashflows` (`amortizacion`, `cupon_interes` per-100 VN) y la hoja `Cashflows` del
  Excel semilla guardan montos en términos BASE, NO nominales-al-pago
  (`core/infrastructure/db/models.py:117`). El motor multiplica por `CER_ref / cer_base` al
  deflactar el precio (TIR real) o al indexar el V.Téc. Flujos ya indexados = doble conteo.
- Bonos con `capital_factor > 1` (DICP / DIP0 / CUAP, reestructurados): el V.Téc se normaliza
  a base 100 dividiendo por la suma de amortizaciones (`pricing/base.py:80-84`).

#### Lag de 10 días hábiles BYMA y cadena de fechas

- `cer_reference_date(settle, lag)` (`core/domain/conventions.py:130-140`) camina hacia atrás
  `lag` días hábiles con `es_habil` (O(1), date-native).
- **TIR real** (`CerStrategy.tir`, `core/domain/pricing/strategies.py:41-59`):
  `precio_real = precio / (CER(settle − 10 hábiles) / cer_base)`; XIRR contra los flujos base
  con la convención declarada (un CER 30/360 descuenta 30/360). `price_from_tir` es la inversa
  (`:61-72`).
- **V.Téc** (`pricing/base.py:74-84`): `(residual + accrued) × CER(ref_CER) / cer_base` con
  `ref_CER = cer_reference_date(settlement_byma_date(ref, settle_lag), cer_lag)` — o sea
  **liquidación T+N PRIMERO, lag CER DESPUÉS**. La misma cadena que el riel CER de
  DUAL_CER_TAMAR (contrato en `pricing/tamar.py:13-48`).
- LECER / BONCER ZC (un solo flujo, `base.py:47-63`): V.Téc base
  `100 × (payoff/100)^(elapsed/total)` (interpolación geométrica emisión → vto) × factor CER.
- **Proyección del CER a futuro** (`pricing/tamar.py:64-77 project_cer_at`): COMPUESTA con el
  crecimiento de los últimos 30 días, `CER_hoy × (1+g)^meses`. Nunca lineal.
- BEI: ajuste γ = `CER_ULT / CER_LIQ−10h` (`core/domain/yield_curve.py:192-203`,
  `apps/cli/bei.py:173-182`).
- Guardianes: `tests/test_rem_R1_financiero_cer_lag.py` (el lag en V.Téc Y en payoff),
  `tests/test_fin_Z1_financiero_vtec_settlement.py` (los dos escalones a la vez).

---

## Bonos TAMAR (PURO, DUAL, DUAL_CER_TAMAR)

- **Fórmula oficial BONTE TAMAR** (`core/domain/pricing/tamar.py:7-11`,
  `core/domain/conventions.py:21-33`): `TAMAR_TEM = ((1 + TNA/k)^k)^(1/12) − 1`,
  `k = 365/32 ≈ 11.40625`. **Capitalización MENSUAL con day-count 30/360**:
  `payoff = 100 × (1 + TEM_max)^N_meses`, `N_meses = days_30_360(emisión, fin) / 30`
  (`tamar.py:189-191`).
- TNA que entra: **promedio aritmético simple** de la TAMAR diaria (BCRA variable 44) sobre
  `[emisión, fin]` (`avg_tamar_tna`, `:88-150`): días ≤ hoy → observada (forward-fill del
  provider); días > hoy → `tamar_forecast` o la última observada. Cache diario por
  `(start, end, forecast, provider)`.
- `spread` (`Instrument.spread_rate`; columnas `spread` / `spread_anual`): anual decimal
  (0.05 = TAMAR + 5 %) sumado a la TNA promedio ANTES de convertir a TEM:
  `tem_tamar = tamar_tem(avg_t + spread)` (`:174-179`).
- **DUAL**: `tasa_fija_mensual` → `Instrument.floor_rate_monthly` (floor MENSUAL decimal);
  `TEM_max = max(TEM_TAMAR, floor)` por mes (`:183-187`).
- **DUAL_CER_TAMAR** (serie TXMJ*): `payoff = max(riel TAMAR, 100 × CER_fin / cer_base ×
  (1 + cer_spread)^años)` con `años` act/365.25 desde la emisión (`:195-216`). Contrato de
  CUATRO pasos, en este orden: **liquidación T+N → lag CER 10 hábiles → spread → max de
  rieles** (docstring `tamar.py:13-48`). El paso 1 corre SOLO en el camino V.Téc
  (`cer_settle_lag=ctx.settle_lag`, `strategies.py:255-269`); el payoff a vencimiento pasa
  `None` porque `end` ya es la fecha de pago. Se rompió dos veces: leer el docstring antes de
  tocar `tamar_dual_payoff_at` o `calculate_technical_value`.
- **TIR** (`TamarStrategy.tir`, `strategies.py:178-196`; `DualCerTamarStrategy.tir`,
  `:280-300`): **TEA nominal cerrada** contra el payoff proyectado,
  `(payoff / precio)^(1/años) − 1`, `años = year_fraction_to(vto, settle)` con la convención
  del bono. `price_from_tir = payoff / (1+tir)^años` (`:207-220`, `:330-343`): inversa exacta,
  el round-trip cierra por construcción. DUAL_CER_TAMAR publica TEA nominal (no real) para
  ser comparable con los DUAL del mismo panel.
- **V.Téc** = el mismo payoff devengado hasta la fecha de referencia (`to_date=ref`;
  `:169-176`, `:255-269`); 100 si el bono todavía no emitió. Para DUAL_CER_TAMAR es el max de
  rieles devengado al settle (fallback: riel CER puro, `:270-277`).
- **MD**: bullet con `m = 12` (ver Modified Duration).
- **Payoff analítico ⇒ fila ANCLA, nunca schedule**: `instrument_groups.ANALYTIC_PAYOFF_TYPES`
  = {PURO, DUAL, DUAL_CER_TAMAR} (`core/domain/instrument_groups.py:54-69`, verificada
  contra el registry por test); la fila `es_ancla=1` la filtra `_orm_to_domain`
  (`core/infrastructure/db/models.py:117-131`). Invariante completo en `CLAUDE.md`.
- **Patas del popup** (`apps/web/bond_detail.py:94-122`): `<TICKER>_TAM` re-valúa como PURO
  (sin floor), `<TICKER>_TF` con TAMAR = 0 (`pricing/stubs.py::ZeroTamar` → siempre el floor),
  `<TICKER>_CER` sin transformación. `FinancialEngine.recompute_as_tamar_puro` /
  `recompute_as_tasa_fija` (`core/domain/services.py:135-158`) hacen lo mismo y hoy no tienen
  callers fuera de la fachada (**no verificado en uso**; el «sufijo `_TAM` en el panel TAMAR»
  del documento viejo ya no existe en `apps/web/panels_rows.py`).
- `tamar_forecast` (decimal, ej. 0.20) en `calculate_tir` / `price_from_tir` /
  `projected_payoff`: override de la TAMAR futura (calculadora del popup).
- **Validación externa** (IAMC, TTJ26): precio 158,20 → V.Téc 146,39 / payback 164,32 /
  TIR EA 39,06 %. Vive SOLO en el docstring (`tamar.py:3-5`); no hay golden ejecutable
  (ver inventario).
- Guardianes: `tests/test_aud_B_financiero_dual_cer_tamar.py` (spread + max + round-trip),
  `test_rem_R1_financiero_cer_lag.py`, `test_fin_Z1_financiero_vtec_settlement.py`,
  `test_rem_R1_financiero_dual_md.py`.

---

## Bonos DOLAR LINKED

- Tipos: `DOLAR_LINKED` (hoja `Dolar_Linked`: letras y bonos del Tesoro — D31M7, D31L6,
  TZV*) y `DOLLAR LINKED` (ON corporativa). `Instrument.is_dolar_linked` acepta las dos
  grafías (`core/domain/models.py:146-150`). **Par = 100 USD**, flujos en USD.
- **Pata pesos** (…O o mono-ticker) → USD dividiendo por el **oficial** (mayorista venta =
  A3500, `fx.get_mayorista_venta`; `pricing/strategies.py:103-111`). Patas …D / …C ya cotizan
  en USD → sin /FX (`:96-101`).
- **V.Téc** = (residual USD + accrued USD) × mayorista para la pata pesos; en USD para …D/…C
  (`:113-126`). **Paridad** = precio / V.Téc (`bond_detail.py:391`).
- **TIR en USD** sobre los flujos reales (maneja amortizables, no asume bullet) con la
  convención declarada (`:128-137`): ON DL → `ACT/365` por default (`repositories.py:396-397`);
  letras DL soberanas → `ACT/365.25` (así van los goldens). Duración vanilla (`m = freq`;
  un solo flujo → 1).
- `tc_inicial` (hoja `Dolar_Linked`) queda en `raw_fields`: `build_instrument` no lo lee y el
  motor no lo usa.
- Goldens (`tests/test_hard_dollar_fx.py:157-193`, calculadora de referencia @ mayorista
  1446,1064, settle 2026-06-10): D31M7 (140.200 ARS → 96,95 USD, TIR 3,92 %) y D31L6
  (143.290 → 99,0868, 6,79 %). Un DL se deflacta por el oficial, NO por MEP/CCL (`:136-154`).

---

## Obligaciones Negociables (ON hard-dollar)

- Tipo `HARD DOLLAR` (categoría fija «Obligaciones Negociables»,
  `core/infrastructure/repositories.py:375-376`), multi-ticker …O (pesos) / …D (MEP) / …C
  (CABLE); panel default …D. Cashflows en USD base 100 generados por
  `core/domain/on_cashflows.py` (cupón anclado al vencimiento y retrocediendo por período;
  **el capital amortiza con su propia cadencia `capital_freq`**, independiente del cupón;
  stubs prorrateados ACT/365 por días reales — AERBO, período final de 175 días).
- **Pata pesos → USD por jurisdicción**: **MEP si Ley Argentina; CCL/cable si Ley Extranjera
  o sin dato** (el universo ON es mayormente ley NY) — `HardDollarStrategy`
  (`pricing/strategies.py:151-161`); `Instrument.is_ley_argentina` (`models.py:158-177`)
  acepta «Argentina», «Ley Local», AR/ARG/ARGY/LOC. dolarapi: `bolsa` = MEP,
  `contadoconliqui` = CABLE (`tests/test_hard_dollar_fx.py:217-235`). Las patas …D / …C se
  usan tal cual, sin FX (`:106-116`). `PROVINCIAL HARD DOLLAR` se precia igual
  (`core/domain/instrument_groups.py:17-23`).
- **Day-count por instrumento**: las ON del CSV (`data/obligaciones_negociables.csv` →
  `core/infrastructure/on_catalog.py:32`, `:93-96`) usan `ACT/365` («real/365», base de
  intereses del calculador del broker) salvo columna `base`; `build_instrument` defaulta
  `ACT/365` para `HARD DOLLAR` / `DOLLAR LINKED` (`repositories.py:396-397`). Bancos cargados
  por ABM: **BACH 30/360** (8 % semestral → 10 cupones de 4,00 exactos) y **BF37 ACT/365**
  (6 % → cupones desiguales 3,0247 / 2,9753) — `test_golden_referencia.py:253-271`;
  BPCV / CACB / CICA `ACT/365` (anclas). BYCV `ACT/365`: **no verificado en código**.
- 30/360 cuenta el accrued desde la fecha PROGRAMADA; ACT/\* desde el día hábil siguiente
  al pago (ver Intereses corridos). TLCPD (Telecom Clase 24, 30/360, amortiza 50 + 50):
  accrued 0,31, no 0,33 (`test_golden_referencia.py:63-96`).
- `serie_clase` («Clase XXXI») es display (`short_name` «EMISOR - Clase X» + `raw_fields`);
  `ley_aplicable` (`ley` / `ley aplicable` / `ley_aplicable`, `repositories.py:400-401`) SÍ
  mueve el pricing de la pata pesos (brecha MEP/CCL en precio, TIR y paridad).
- `on_catalog.ingest()` es DESTRUCTIVO (borra la hoja y la reconstruye del CSV): las ON de
  la ABM se editan por la ABM, nunca por re-ingesta (invariante en `CLAUDE.md`).

---

## Soberanos: 3 especies por moneda (ARS / MEP / CABLE) + pricing de la pata ARS

- BONAR / GLOBAL / BOPREAL: **mismo flujo, 3 tickers** — ARS sin sufijo (AL30), MEP `D`
  (AL30D), CABLE `C` (AL30C); BOPREAL pesos = base BPO* (BPOC7). AO27D / AO28D son solo MEP.
  Moneda por sufijo: `core/domain/currency.py`. Ya no se ocultan las patas pesos/cable
  (refutado el viejo «filtro MEP-only»).
- **Pricing de la pata ARS**: precio USD implícito = `pesos ÷ offer` — **CABLE si GLOBAL (ley
  NY); MEP si BONAR o BOPREAL (ley local)** (`core/use_cases/generate_report.py:99-105`,
  `:138-155`). Vive en `_enrich_metrics`, FUERA del motor (para no entrar al perímetro de
  equivalencia): TIR / V.Téc / MD / paridad sobre el USD implícito; la columna Precio muestra
  los pesos sin decimales. Refutado: TIR −94 % / paridad 130.000 % de la pata peso.
- BOPREAL descuenta **30/360** aunque la fila no lo declare (`models.py:192-194`, `:213`;
  `repositories.py:394-395`).
- Soberanos mid-amort con solo flujos futuros cargados: período corriente inferido (ver
  Intereses corridos).
- ABM: las 3 especies se consolidan en UN bono (form con los 3 tickers,
  `SHEET_SCHEMAS["Soberanos"]`, `apps/web/instruments_abm.py:186-214`); siguen siendo filas
  independientes en SQLite y el motor las precia por ticker.

---

## Bonos LECAP / BONCAP capitalizables

- **Síntesis del payoff** (`cashflow_synth._synth_lecap_boncap`,
  `core/domain/cashflow_synth.py:135-153`): flujo único al vencimiento
  `100 × (1 + tem_licit)^N_meses`, **`N_meses = days_30_360(emisión, vto) / 30`** (30/360,
  Secretaría de Finanzas). Si `base calculo` contiene «act» usa días corridos / 30. Sin
  `tem_licit` → 100 al vto (no crashea, pero es un dato ausente: cargarlo).
- Goldens (`tests/test_cashflow_synth.py:46-91`): **S29Y6** (TEM 2,35 %, 2025-05-30 →
  2026-05-29: 359 días 30/360 → 11,97 meses → **132,0438**; con días corridos daría 132,57 —
  el bug histórico que mostraba TNA 33,88 % contra 21,09 % de la referencia) y **S15S6**
  (TEM 1,99 %, base vacía → 30/360 → **107,21**).
- **V.Téc(t)** = `100 × (payoff/100)^(elapsed/total)` con días corridos entre emisión y vto
  (`pricing/base.py:47-63`); la misma rama sirve a LECER / BONCER ZC (× factor CER).
- **TIR**: TEA cerrada de 2 flujos (`xirr.py:67-119`) descontando con la convención
  declarada (default `ACT/365.25` si la fila no trae base). El 30/360 de la síntesis y la
  base de descuento son dos convenciones distintas (pago vs descuento).
- **TNA / TEM**: el popup muestra las cuatro variantes (`tea_to_tna` base 365,
  `tea_to_tna_monthly` m=12 = «Tir Nominal» IAMC, `tea_to_tem` 30/365, `tea_to_tem_m12`
  30/360 Sec. Finanzas) — `bond_detail.py:405-408`. Fórmulas en Conversión de tasas.
- Settlement **T+1** como todos (refutado el T+0 para LECAP/BONCAP/LECER/CI).
- `tem_licit` también cae en `Instrument.floor_rate_monthly` (`repositories.py:378-379`):
  inocuo fuera de DUAL (nadie lo lee ahí).
- Alta automática de letras desde ArgentinaDatos: un `tem: 0` es dato AUSENTE, no una tasa
  de cero; sin `fechaEmision` no hay alta (invariante en `CLAUDE.md`).

---

## Futuros DLR, valor relativo y escenarios

- **Futuros DLR (Matba/Rofex)** — `apps/web/panels_rows.py:295-315 _implied_rates`:
  TNA lineal base 365 `(F/S − 1) × 365/d` (= informe A3 de Matba y popup Curva Rofex);
  TEA `(F/S)^(365/d) − 1` (`core/infrastructure/futures_provider.py:104-116 implied_tea`);
  crawl mensual `(F/S)^(30/d) − 1`. Spot = BCRA A3500 (variable 5). La efectiva compuesta es
  la TEA, no la TNA.
- **Valor relativo** (`panels_rows.py:55-95`): ajuste `TIR = a + b·ln(MD)` por grupo, solo
  curvas peso de flavor único (Tasa Fija nominal, CER real — NO soberanos multi-moneda);
  `spread_curva = TIR − TIR_curva` (+ barato / − caro); `carry_roll = TEM + roll-down`
  con roll de 1 mes.
- **Escenarios** (`core/domain/scenarios.py`): `ΔP/P ≈ (1 − MD·Δy + ½·C·Δy²) × (1 + β_precio·ΔFX) − 1`;
  betas: hard-dollar USD (β_precio 0, β_valor 1), dólar-linked (1, 0), pesos (0, 0). Sin
  convexidad publicada, lineal en MD.

---

## Curvas y BEI (NT N°3/2019 + NT N°8/2024)

`core/domain/yield_curve.py` (docstring `:1-16`):

| Función | Origen |
|---|---|
| `NelsonSiegelCurve` (4 params) | NT8 Eq. 11 |
| `NelsonSiegelSvenssonCurve` (6 params) | NT3 Eq. 17 |
| `bootstrap_zero_rates(bonds, today)` | NT3 Eq. 11-16 (tenores en años act/365.25) |
| `fisher_break_even(i, r)` = `(1+i)/(1+r) − 1` | NT8 Eq. 8 (`:141-143`) |
| `gamma_known_cer_factor(cer_liq, cer_last)` = `CER_ULT / CER_LIQ−10h` | NT8 Eq. A4 (`:192-203`) |
| `forward_rate(curve, t1, t2)` | NT3 Eq. 8'/9' (`:146-160`) |
| `forward_bei_between_tenors(...)` | NT3 Eq. 10 (`:163-177`) |
| `pair_delta(...)` → δ − 1 | NT8 Apéndice Eq. A13 (`:300-335`) |
| `pair_monthly_inflation(δ−1, días)` | NT8 Eq. A12 (`:338-349`) |
| `real_fx_drift(dev, infl)` = `(1+dev)/(1+infl) − 1` | Fisher sobre FX (`:180-189`) |
| `inflation_path.monthly_inflation_path(nom, real, today, months_ahead=12)` | NT8 Fig. 4 (`core/domain/inflation_path.py:42-55`) |

- Monitor BEI (`apps/cli/bei.py`, docstring `:1-25`): tenores estándar 3M / 6M / 9M / 1Y /
  18M / 2Y / 3Y (`:114-115`); bootstrapping → NSS con fallback NS y lineal → Fisher →
  sendero mensual vs mediana REM-BCRA → método de pares LECAP/CER con vencimientos a ≤ 35
  días (`:226`) → tercer riel TAMAR → devaluación implícita DLR → TC real → ajuste γ
  (`:173-182`) → persistencia `bei_diario.csv`. Tenores en años, tasas decimales.
- «`pair_delta` da exactamente 3,81 % para S14F5/T2X5»: **no verificado en código**.

---

## Política de precisión y tolerancias

Verificada 2026-09-07 sobre HEAD `c0dda9d`.

- **float de punta a punta, sin `Decimal`**: cero ocurrencias de `Decimal` en `core/`,
  `apps/` y `config/` (la única aparición es la palabra en un `help` del ABM).
- **Sin redondeo en el core**: cero `round()` en `core/domain/pricing/*`, `xirr.py`,
  `daycount.py`, `conventions.py`, `models.py`, `cashflow_synth.py`, `services.py`. El
  redondeo es de display / infra (templates, `bei.py` al serializar, `on_service`, panels).
- **Tolerancias canónicas**:

| Qué | Tolerancia | Dónde |
|---|---|---|
| Equivalencia motor nuevo vs legacy congelado | **1e-7 relativa** (`abs(a−b) ≤ 1e-7 × max(1, \|a\|, \|b\|)` — absoluta por debajo de 1) | `tests/test_pricing_equivalence.py:119-129 _close` |
| Golden TIR vs calculadora de referencia | **1,5 bp** (`abs=1.5e-4`) | `tests/test_golden_referencia.py:203-206`; DL `test_hard_dollar_fx.py:187` |
| Golden clean / accrued / V.Téc / MD | 1e-3 / 2e-3 / 1e-2 / 1e-2 | `test_golden_referencia.py:209-228` |
| Newton (pre-paso) | acepta solo finito, `> −1`, **`\|NPV\| < 1e-4`** | `core/domain/xirr.py:35`, `:206` |
| brentq | **`xtol=1e-12`**, `maxiter=200` | `xirr.py:146` |
| Forma cerrada de 2 flujos vs root-finder | **≤ 1e-9 relativo** (medido 9,4e-13 sobre 1.158 instrumentos × 3 precios) | `xirr.py:95-102`; `tests/test_perf_W1_dominio_xirr.py:109-125` |
| Memo por instrumento de `metrics` | igualdad EXACTA | `tests/test_perf_W1_dominio_metrics_memo.py` |
| Solver contra yields conocidos | 1e-6 … 1e-3 abs según el caso | `tests/test_xirr_solver.py` |
| Payoff LECAP golden | 1e-3 (S29Y6), 1e-2 (S15S6) | `tests/test_cashflow_synth.py:61`, `:78` |
| Pata pesos vs pata USD (mismo bono) | 1e-9 abs | `test_hard_dollar_fx.py:96`, `:154` |

- **Fecha fija de tests**: 2026-06-10 (`tests/_clock.py`, override `MONITOR_TEST_REF_DATE`);
  `MONITOR_AS_OF` congela `today()` del dominio (`core/domain/clock.py`) — solo tests.
- **Degeneración**: TIR ≤ −1 → `None`; overflow → `None`/`NaN`; nunca una excepción sube
  desde el pricing.
- **Cero = dato ausente** en los bordes de ingesta (precio 0 de la fuente activa, `ccp ≤ 0`
  de FCI, `tem: 0` de letras): se descarta, no se usa como valor.
- **Perímetro de la equivalencia**: el motor legacy (`tests/_legacy_engine.py`) comparte A
  PROPÓSITO `_xirr_from_years`, `pricing.metrics` y `days_30_360` con producción; esos tres
  no los cubre la equivalencia sino `test_xirr_solver.py`, `test_golden_referencia.py` y
  `test_cashflow_synth.py`. **No copiar símbolos vivos al legacy.** La equivalencia detecta
  regresiones, no errores de origen: para eso están los goldens externos.

---

## Verificación independiente: inventario de goldens

Estado 2026-09-07. «Procedencia» = fuente externa, fecha y captura. **Regla**: todo golden
nuevo declara procedencia en su docstring (`agents.md §0.1.14`); sin procedencia no es
golden, es una foto del motor.

| Familia | Cantidad | Instrumentos | Fuente externa | Procedencia | Test |
|---|---|---|---|---|---|
| ON hard-dollar (TIR, clean, accrued, V.Téc, MD) | **13** | CLISA (CLSIO), TLCPD, YM42D + anclas CICA, CACB, BPCV, BF40, CACD, OZC3, PLC4, PN35, YM37, TTC8 | «la calculadora de referencia» = la calculadora de bonos del broker del autor (nombre anonimizado a propósito en a0c2e5f), settles 2026-06-01 / 2026-06-10 | declarada en el docstring del test; sin capturas | `tests/test_golden_referencia.py` |
| Cashflows sintetizados de ON (bancos) | 2 | BACH (30/360), BF37 (ACT/365) | ídem | pendiente | `test_golden_referencia.py:253-271` |
| Dólar-linked soberano (TIR en USD, USD implícito) | **2** | D31M7, D31L6 | calculadora de referencia @ mayorista 1446,1064, settle 2026-06-10 | pendiente | `tests/test_hard_dollar_fx.py:157-193` |
| LECAP (payoff sintetizado) | **2** | S29Y6 (132,0438), S15S6 (107,21) | referencia oficial (TNA 21,09 % de S29Y6) | pendiente | `tests/test_cashflow_synth.py:46-78` |
| Calendario y `cer_base` contra BCRA | **6 fechas + 2 cer_base** | 4 feriados + 1 fecha espuria + 1 liquidación T+1; TZXS7/TZXS8/TZXM8 (723.06 → 2026-03-13), X29Y6/TZXA7 (651.89806 → 2025-11-12) | argentinadatos / Boletín Oficial; BCRA variable 30 | declarada en el docstring | `tests/test_holiday_calendar.py` |
| **CER (TIR real / paridad / V.Téc / MD)** | **1 corte + 2 intradía** | TX28 @ 1.719 (cierre 24hs BYMA 2026-09-03): TIR 8,82 % · paridad 93,14 % · MD 1,08 → motor 8,822 % / 93,140 % / 1,081. Secundarios 2026-09-07: Bonistas @1738 (8,11 % / 93,89 % / VT 1851,03) y Docta @1737,50 | Banco Hipotecario, Informe Diario (PDF público); Bonistas.com; Docta | **declarada** en el docstring y en `tests/fixtures/tx28_2026-09-03.json` (valores textuales, URLs, serie CER BCRA var 30, fecha de captura, qué NO declara cada fuente) | `tests/test_golden_tx28.py` (17 tests; discrimina T+0, lag 0, day-count y CER ±1 %) |
| **TAMAR / DUAL / DUAL_CER_TAMAR** | **0** ejecutables | TTJ26: precio 158,20 → V.Téc 146,39 / payback 164,32 / TIR EA 39,06 % | IAMC | solo docstring `core/domain/pricing/tamar.py:3-5` | — |

- **BONCER cerrado (2026-09-07)**: el golden de TX28 confirma que la TIR publicada por el
  mercado para un CER es **real sobre CER y efectiva anual**, y que el V.Téc usa el CER de
  10 hábiles antes de la liquidación **T+1** con day-count 30/360 (T+0 → 8,735 %; sin lag →
  9,733 %; ACT/365 → paridad 93,00 %: todos fuera de tolerancia). TX26 no sirve: vence
  2026-11-09 con un solo flujo.
- **TAMAR sigue pendiente**: TTJ26 venció el 2026-06-30 y el ancla IAMC del docstring no se
  puede reproducir sin la serie TAMAR/CER de ese día. Sustitutos vivos con corte del mismo
  Informe Diario (2026-09-03): TTS26 @169,10 → TIR 22,56 % / paridad 100,33 % / MD 0,03;
  TTD26 @169,00 → 24,78 % / 100,27 % / 0,27. IAMC no publica el informe desde 2026-05-26 y su
  feed en BYMA open está paywalleado.
- Lo que sí cubre el resto de la red sin oráculo externo: equivalencia (todo el universo),
  invariantes property-based (round-trip, monotonía, cotas), goldens internos de calendario.

---

## Schema de las hojas / campos del instrumento

**SQLite `catalog.db` es la FUENTE DE VERDAD; el Excel `data/instruments_master.xlsx` y el
CSV `data/obligaciones_negociables.csv` son SEMILLAS de bootstrap** (se leen solo si la DB
está vacía; invariante en `CLAUDE.md`). Las «hojas» sobreviven como concepto lógico: cada
fila de `instruments` guarda `sheet` + `raw_fields` (JSON con los parámetros crudos del
form) para que el ABM haga round-trip (`core/infrastructure/db/models.py:5-8`, `:96-98`).
Las altas y ediciones van por el ABM (`apps/web/instruments_abm.py`, `SHEET_SCHEMAS`
`:186-345`); el parser compartido Excel/ABM es `repositories.build_instrument`
(`core/infrastructure/repositories.py:356-413`), el ÚNICO que decide el `instrument_type`.

### Tablas

| Tabla | Columnas (dominio) | Notas |
|---|---|---|
| `instruments` | `ticker` (PK, pata principal), `ticker_mep`, `ticker_ccl`, `short_name`, `instrument_type`, `isin`, `maturity_date`, `emission_date`, `cer_base`, `cer_lag` (10), `category`, `floor_rate_monthly`, `spread_rate`, `cer_spread`, `payment_frequency` (2), `day_count` (`ACT/365.25`), `sheet`, `raw_fields` | `db/models.py:73-105`. Multi-ticker: al cargar se expande a una especie por ticker (moneda por sufijo). |
| `cashflows` | `ticker` (FK), `fecha_pago`, `amortizacion`, `cupon_interes`, `es_ancla` | per-100 VN en términos **BASE**. `es_ancla=1` = fila de vencimiento de un payoff analítico (montos 0), filtrada por el motor. |

### Hojas lógicas (ABM) y campos → dominio

| Hoja | Tipos (`tipo` / `clase`) | Campos propios | Notas |
|---|---|---|---|
| **Soberanos** | BONAR, GLOBAL, BOPREAL | `ticker_ars` / `ticker_mep` / `ticker_ccl` (≥ 1), `short_name`, `fecha_emision`, `fecha_vencimiento`, `cupon anual %` (step-up `'2024-12-31:0.63;2027-12-31:1.18'`), `frecuencia pagos`, `base calculo`, `tipo amortizacion` | Sin flujos explícitos, el PREVIEW del ABM sintetiza un bullet regular (`cashflow_synth`); amortizing / 1er cupón irregular van EXPLÍCITOS. BOPREAL → 30/360. |
| **Tasa_Fija** | LECAP, BONCAP, BONOFIJA | `fecha_emision`, `fecha_pago` (= vto), `tem_licit` (decimal, LECAP/BONCAP), `cupon anual %` + `frecuencia pagos` (BONOFIJA), `base calculo` | LECAP/BONCAP: payoff `100 × (1+tem)^N` con N en 30/360. BONOFIJA usa flujos explícitos. |
| **CER** | LECER, BONCER, BONCER ZC, CON CUPON, STEP-UP | `fecha emision`, `fecha vencimiento`, `cupon anual %`, `frecuencia pagos`, `base calculo`, `tipo amortizacion`, **`cer emision`** (CER 10 hábiles pre-emisión), `categoria` (etiqueta de mercado, ej. «BONCERES CERO CUPON») | `cer emision` es crítico (default 1.0 = sin deflación). `dias habiles previos` / `dias_lag` → `cer_lag` (10). |
| **Dolar_Linked** | (sin columna `tipo` → `DOLAR_LINKED`) | `fecha_emision`, `fecha_vencimiento`, `cupon anual %` (vacío = zero-coupon), `frecuencia pagos`, `base calculo`, `tipo amortizacion`, `tc_inicial` | Par 100 USD; V.Téc en pesos = USD × mayorista. `tc_inicial` no entra al pricing. |
| **TAMAR** | PURO, DUAL, DUAL_CER_TAMAR | `fecha_emision`, `fecha_vencimiento`, `tasa_fija_mensual` (floor mensual, DUAL), `spread` (anual sobre TAMAR), `cer_base` + `cer_spread` (DUAL_CER_TAMAR), `base calculo` (30/360 por documento BONTE) | Payoff analítico: **sin schedule**, fila ancla. `cupon anual %` / `frecuencia pagos` figuran en el form pero el payoff no los usa. |
| **Obligaciones_Negociables** | HARD DOLLAR, DOLLAR LINKED | `short_name` (emisor), `serie_clase`, `sector_override`, `ley_aplicable` (Argentina / Extranjera), `fecha_emision`, `fecha_vencimiento`, `cupon anual %`, `frecuencia pagos`, `base calculo` (default ACT/365), `tipo amortizacion`, `denom_base` / `denom_incremento` / `valor_nominal` (display) | Default de hoja **AMBIGUO**: sin `tipo` se asume HARD DOLLAR con WARNING (`repositories.py:212-229`) — una DL sin tipo se preciaría en la moneda equivocada. Flujos USD por `on_cashflows`. |
| **Acciones** | ACCION | — | Sin flujos ni pricing de bono (Panel Líder cotiza por ticker). |
| Provinciales | PROVINCIAL HARD DOLLAR, PROVINCIAL ARS / CER / DOLAR_LINKED | flujo explícito | Tipos propios para tener panel propio (`instrument_groups.py:17-28`); HD se precia como ON. |
| `Cashflows` / `Cashflows_Fija` (Excel semilla) | — | `ticker`, `fecha_pago`, `amortizacion`, `cupon_interes` / `monto` | Sobreescriben la síntesis; per-100 VN en base. `NON_INSTRUMENT_SHEETS` (`repositories.py:440`). |

### Alias de columnas que entiende `build_instrument` (`repositories.py:356-413`)

| Campo del dominio | Columnas aceptadas (en orden) | Default |
|---|---|---|
| `instrument_type` | `tipo`, `clase` | default de hoja (`_SHEET_DEFAULT_TYPE`, `:212-216`): Obligaciones_Negociables → HARD DOLLAR (ambiguo, WARNING), Dolar_Linked → DOLAR_LINKED, Acciones → ACCION; otra hoja sin tipo → huérfano (WARNING, invisible). Debe existir en `instrument_groups` (`is_known_type`). |
| `maturity_date` | `fecha_vencimiento`, `fecha vencimiento`, `fecha_pago`, `maturity` | — |
| `emission_date` | `fecha_emision`, `fecha emision` | — |
| `cer_base` | `cer emision`, `cer_emision`, `cer_base` | 1.0 |
| `cer_lag` | `dias habiles previos`, `dias_lag` | 10 |
| `floor_rate_monthly` | `tasa_fija_mensual`, `tem_licit` | None |
| `spread_rate` | `spread`, `spread_anual` | None |
| `cer_spread` | `cer_spread`, `spread_cer` | None |
| `payment_frequency` | `frecuencia pagos`, `frecuencia` | inferida del gap mediano de los flujos (`:21-40`) |
| `day_count` | `base calculo`, `base_calculo` | BOPREAL 30/360 · ON ACT/365 · resto ACT/365.25 |
| `ley_aplicable` | `ley_aplicable`, `ley aplicable`, `ley` | None (→ CCL en hard-dollar) |
| `isin` | `isin`, `codigoisin`, `codigo_isin` | None (display) |
| `category` | `categoria` | «Obligaciones Negociables» para ON |

- **Fechas**: `_parse_date` detecta strings ISO (`YYYY-MM-DD`) y los parsea con
  `dayfirst=False` (`repositories.py:92-110`); con `dayfirst=True` pandas swapeaba mes/día
  (`2026-07-09` → `2026-09-07`). `cashflow_synth._get_date` normaliza `datetime` /
  `pd.Timestamp` → `date` (`cashflow_synth.py:58-90`) para que el motor no compare tipos
  mixtos.
- **La ABM ya no sintetiza al guardar**: la síntesis es PREVIEW (`POST /abm/preview_cashflows`)
  porque `cashflow_synth` lee el reloj para el step-up del cupón; un tipo normal sin flujos se
  rechaza (invariante en `CLAUDE.md`).

---

## Cómo extender la matemática financiera

`FinancialEngine` (`core/domain/services.py`) es una **fachada delgada** que preserva las
firmas públicas y delega: dispatch por tipo → `pricing/registry.strategy_for`; cálculo →
`PricingStrategy.{technical_value, tir, duration, price_from_tir}`; métricas de popup →
`pricing/metrics`; conversiones → `conventions`; XIRR → `xirr`. **La matemática nueva NO va
en la fachada.**

### Métodos públicos vigentes (firmas preservadas)

| Método (`services.py`) | Devuelve |
|---|---|
| `xirr(flows, dates)` (`:52-54`) | TIR (decimal) con años julianos 365.25 |
| `calculate_technical_value(snapshot, indices, fx=None, ref_date=None, settle_lag=1)` (`:56-65`) | V.Téc universal (residual + accrued; ramas CER / DL / TAMAR / DUAL_CER_TAMAR por strategy) |
| `calculate_tir(snapshot, indices=None, fx=None, settle_date=None, tamar_forecast=None)` (`:67-77`) | TEA; `settle_date` = override T+0/T+1 |
| `calculate_duration(snapshot, tir, settle_date=None)` (`:79-89`) | MD (m = freq; TAMAR-family m = 12) |
| `price_from_tir(snapshot, tir, indices, fx, settle_date, tamar_forecast)` (`:91-104`) | precio dirty implícito |
| `tir_from_price(snapshot, price_override, ...)` (`:106-116`) | inversa de `calculate_tir` vía `model_copy` del snapshot |
| `projected_payoff(instrument, indices, tamar_forecast, ref_date)` (`:121-133`) | payback per-100 a vto (TAMAR-family) |
| `recompute_as_tamar_puro(snapshot, indices)` / `recompute_as_tasa_fija` (`:135-158`) | (tir, vtec, md) re-valuando un DUAL como PURO / solo floor |
| `calculate_theoretical_price(instrument, tir, ref_date)` (`:163-180`) | PV con la convención declarada |
| `calculate_pct_change(current, previous)` (`:182-186`) | variación % (None-safe, eps 1e-12) |
| `accrued_interest`, `days_since_last_coupon`, `residual_nominal`, `current_yield`, `dv01`, `convexity` (`:191-221`) | delegan a `pricing/metrics` |
| `tea_to_tem`, `tea_to_tna_monthly`, `tea_to_tna`, `tea_to_tna_freq`, `tea_to_tem_m12` (`:226-244`) | delegan a `conventions` |

### Pasos para una familia nueva

1. **El tipo primero**: agregarlo a `core/domain/instrument_groups.py` (un tipo que no está
   ahí deja el bono INVISIBLE; invariante en `CLAUDE.md`). Si su payoff es de fórmula cerrada,
   también a `ANALYTIC_PAYOFF_TYPES` — un test lo verifica contra el registry.
2. **Predicado en el modelo**: property `is_<familia>` en `core/domain/models.py`, sobre
   `norm_type` (los predicados matchean por SUBSTRING; los paneles por igualdad exacta).
3. **Strategy**: clase en `core/domain/pricing/strategies.py` que sobreescribe SOLO su rama y
   delega a `super()` (`VanillaStrategy`) cuando su guarda no se cumple. Regla nueva en
   `pricing/registry.py::_RULES` — el orden es la precedencia (más específico primero).
4. **Descontar SIEMPRE con `inst.year_fraction_to`** (nunca 365.25 cableado) y usar
   `metrics.discount_year_fractions` (stub incluido). Fechas por `today()` del dominio
   (`core/domain/clock.py`), nunca `date.today()` en el core.
5. **Conversión de tasas nueva**: en `conventions.py`, con su espejo en `FinancialEngine`
   si algún consumidor la necesita por la fachada.
6. **Tests**: golden externo CON procedencia (fuente, fecha, captura) + test guardián que se
   ponga ROJO al revertir el cambio (prueba por mutación, `agents.md §0.1.8`) + invariantes
   (round-trip `price → tir → price`, monotonía, cotas de MD) si aplica.
7. **Equivalencia**: `tests/test_pricing_equivalence.py` compara TODO el universo cargado
   contra `tests/_legacy_engine.py` (congelado). Una familia nueva entra al perímetro
   explícitamente (se decide y se documenta cómo), y **nunca** copiando símbolos vivos al
   legacy.
8. **Documentar acá** (una línea con `archivo:línea`), no en `CLAUDE.md` ni en `agents.md`.

**Nunca**: reimplementar TIR / duration / NPV / accrued fuera de `pricing/`; re-sortear
cashflows (el modelo ya los ordena, `models.py:117-124`); usar `round()` en el core; tratar
un `0` de una fuente externa como valor; hardcodear tickers en un monitor.

---

## Refutado — no volver a escribir

Afirmaciones del documento viejo que el código contradice. Se listan para que no
reaparezcan por copia:

| Afirmación vieja | Vigente | Evidencia |
|---|---|---|
| Rail TAMAR diario `(1 + (TAMAR_d + spread)/365)` | Capitalización MENSUAL `(1+TEM_max)^N_meses`, N en 30/360 | `tamar.py:7-11`, `:189-191`; `conventions.py:21-33` |
| T+0 (CI) para LECAP / BONCAP / LECER / «CI»; T+1 el resto | **T+1 para todos**; el T+0 es solo el toggle del popup | `conventions.py:104-121` |
| CER proyectado linealmente | Compuesto `(1+g)^meses` | `tamar.py:64-77` |
| El Excel `instruments_master.xlsx` como fuente de verdad viva; «¿Agregaste un instrumento? Solo en el Excel» | SQLite `catalog.db` es la verdad; Excel/CSV semillas; altas por ABM | `CLAUDE.md` invariantes; `db/models.py` |
| Accrued desde `emission_date` cuando no hay flujos pasados | Período inferido (`prev = próximo − 12/freq meses`) salvo primer período genuino | `metrics.py:33-63` |
| Bracket fijo `[−0.999, 10]` del solver | brentq con auto-bracketing hasta `2^60` + forma cerrada de 2 flujos | `xirr.py:37-50`, `:122-148` |
| Descuento a 365.25 para todo lo no-30/360 | Convención DECLARADA (`year_fraction`); ONs ACT/365 | `daycount.py`; CICA 7,56 % |
| Filtro «solo MEP» en soberanos | 3 monedas visibles; pata ARS dolarizada por MEP/CABLE según ley | `generate_report.py:138-155` |
| DUAL_CER_TAMAR con TIR real y MD m=1 | TEA nominal contra max de rieles; MD m=12 | `strategies.py:223-343` |
| Sufijo `_TAM` como fila del panel TAMAR | Patas `_TAM` / `_TF` / `_CER` del popup | `bond_detail.py:94-122` |

---

## No verificado en código

Se conservan como referencia, sin test ni línea que los respalde:

- Ejemplo del paper NT8/2024 para `cer_base`: T2X5, emisión 14/03/2023 → CER del
  28/02/2023 = 81.22.
- «`pair_delta` da exactamente 3,81 % para S14F5/T2X5».
- BYCV con `ACT/365` (los otros bancos sí tienen ancla o golden de cashflows).
- Uso en runtime de `FinancialEngine.recompute_as_tamar_puro` / `recompute_as_tasa_fija`
  (existen, sin callers fuera de la fachada; el popup usa `_apply_leg`).
- Procedencia de «la calculadora de referencia» de los 17 goldens (13 ON + 2 DL + 2 LECAP):
  fuente, fecha y captura no están registradas en el repo (pendiente Fase 5).
- Validación IAMC de TTJ26 (158,20 → 146,39 / 164,32 / 39,06 %): solo docstring.
