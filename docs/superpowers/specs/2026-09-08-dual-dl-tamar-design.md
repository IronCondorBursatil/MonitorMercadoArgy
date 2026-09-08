# DUAL dólar-linked / TAMAR (`DUAL_DL_TAMAR`) — diseño

**Fecha:** 2026-09-08 · **Estado:** aprobado en brainstorming (enfoque A) · **Caso:** TMVE8

## 1. Qué se construye y por qué

TMVE8 («Bono del Tesoro Nacional en moneda dual TAMAR / dólar linked», emisión
2026-07-31, vencimiento 2028-01-31, ISIN AR0821229090) apareció en la pestaña Novedades
del ABM y no se puede cargar: el motor no tiene un tipo para su payoff. Según la ficha
BYMA, al vencimiento paga el máximo entre:

1. el valor nominal convertido a pesos al **tipo de cambio aplicable** (riel dólar-linked), y
2. el valor nominal convertido a pesos al **tipo de cambio inicial** más los intereses a la
   TAMAR TEM capitalizable mensualmente (riel TAMAR).

Es el gemelo estructural de `DUAL_CER_TAMAR` (serie TXMJ*): el mismo riel TAMAR contra un
riel indexado, sólo que el índice es el dólar mayorista (A3500) en vez del CER, sin lag de
10 hábiles y sin spread sobre el índice.

Decisiones tomadas con David (2026-09-08):

- La TIR del panel es **TEA nominal en pesos**, comparable con los duales TAMAR con los que
  comparte panel; el popup agrega una pata `_DL` con la **TIR en USD** del riel dólar-linked.
- El riel dólar-linked usa el **dólar del settle, sin proyectar** (misma convención que el
  V.Téc dólar-linked). Proyectar por futuros DLR o un «TC esperado» manual queda fuera.
- Enfoque A: strategy propia que **reusa** `tamar_dual_payoff_at` para el riel TAMAR y no
  toca el contrato de cuatro pasos del dual CER.
- PR17 (Bocon BADLAR) **no** entra: se carga por ABM como `PROVINCIAL ARS`, igual que TB27.

## 2. Dominio y tipo

- `core/domain/instrument_groups.py`: grupo PROPIO `DUAL_DL = ["DUAL_DL_TAMAR"]`, sumado a
  `BOND_TYPES`, `KNOWN_TYPES` y `ANALYTIC_PAYOFF_TYPES` (`has_closed_form_payoff` → fila
  ancla, sin schedule). **No** va dentro de `DUAL_TAMAR` (ajuste al escribir el plan): el
  relevamiento mostró que `apps/cli/bei.py` toma `DUAL_TAMAR` como universo de TEA en pesos
  «pura» y un riel dólar la distorsionaría, y que el panel `tamar`
  (`routers/panels_schema.py`), la curva `tamar` (`routers/curva.py`) y el test del ancla
  tienen el trío `{PURO, DUAL, DUAL_CER_TAMAR}` cableado. Entra explícitamente al panel
  TAMAR/Dual, a `apps/web/app.py::_ALL_TYPES` y al grupo «TAMAR» de la cartera; queda fuera
  de BEI y de la curva `tamar` a propósito.
- `core/domain/models.py`:
  - `Instrument.fx_base: Optional[float] = None` — tipo de cambio inicial (pesos/USD). Sale
    de `raw_fields["tc_inicial"]`, el mismo campo que ya usa la hoja Dólar Linked. Lo mapea
    `catalog_repository._orm_to_domain` para toda fila que lo traiga (en un `DOLAR_LINKED`
    es inerte: su strategy no lo lee).
  - `Instrument.is_dual_dl_tamar` → `norm_type == "DUAL_DL_TAMAR"`.
  - `is_cer` ya excluye todo tipo que contenga «TAMAR»; `is_dolar_linked` no matchea
    («DUAL_DL_TAMAR» no contiene «DOLAR»). Se pinea con tests.
- Day-count declarado 30/360, como todo TAMAR (`day_count_enum` lo aplica igual).

## 3. Pricing: `DualDlTamarStrategy`

Vive en `core/domain/pricing/strategies.py`; `registry._RULES` la registra **antes** de la
regla TAMAR (`is_tamar_puro or is_dual_tamar`), disjunta de todas las demás.

> **Corrección tras la revisión final (2026-09-08).** El diseño original asumía que TMVE8
> cotizaba per 100 VN **en pesos**, como los duales TAMAR, y por eso normalizaba el riel
> dólar-linked a `100 × FX / fx_base`. **Es al revés**: TMVE8 cotiza en **pesos por 100 VN
> denominados en USD**, como los dólar-linked. Data912 del 2026-09-08 → TMVE8 `c=139680`,
> TZVD8 `118650`, D31M7 `147800`, contra TTS26 `169,5`, TMF27 `121,75`, TXMJ8 `99,5`; la
> ficha BYMA dice «moneda: Dólares» y «el Valor Nominal emitido convertido a Pesos». Las dos
> patas del payoff van entonces en esa escala: el riel DL son `100 × FX` y el riel TAMAR es
> la capitalización per-100 llevada a pesos por el TC inicial (`fx_base × T`). Lo de abajo ya
> está corregido.

Definiciones (per 100 de VN **denominado en USD**, o sea en pesos; `settle` = fecha de
liquidación del contexto):

- **Riel TAMAR** `T(d)` = `tamar_dual_payoff_at(inst, ref, indices, to_date=d,
  tamar_forecast=ctx.tamar_forecast)`. Para este tipo la función devuelve exactamente
  `100 × (1 + TEM_TAMAR+spread)^N_meses` capitalizado desde la emisión (per 100 USD): no es
  `DUAL` (sin floor) ni `DUAL_CER_TAMAR` (sin riel CER). **No se modifica** `tamar.py`.
- **FX** = `fx.get_mayorista_venta()` (dolarapi, el mismo que `DolarLinkedStrategy`); si no
  hay dato **o el dato es ≤ 0**, `indices.get_a3500(settle)` (fixing BCRA, con forward-fill);
  si tampoco, None.
- **Riel DL** `DL` = `100 × FX` (los 100 USD del VN al dólar del settle). Es un solo número
  para el settle: no se proyecta.
- **Riel TAMAR en pesos** = `fx_base × T(d)` (los 100 USD capitalizados a TAMAR, valuados al
  **TC inicial** del prospecto — que es lo que dice la ficha BYMA).

Métricas:

| Métrica | Fórmula |
|---|---|
| V.Téc | `max(fx_base × T(settle), DL)`; antes de la emisión, `100 × fx_base` |
| Payoff proyectado | `P = max(fx_base × T(vencimiento), DL)` |
| TIR (TEA nominal) | `(P / precio)^(1/años) − 1`, con `años = inst.year_fraction_to(vto, settle)` y el **precio en pesos por 100 VN USD** (≈139.680 el 2026-09-08) |
| MD | bullet `años / (1 + TIR)^(1/12)` (m=12, igual que PURO/DUAL/DUAL_CER_TAMAR) |
| `price_from_tir` | `P / (1 + TIR)^años` — inversa exacta: el round-trip cierra por construcción |

Bordes: `fx_base` ausente o ≤ 0 → no hay escala en pesos y **no se inventa** (V.Téc —incluido
el pre-emisión— y TIR devuelven None, no el riel TAMAR solo: un dual sin su segundo riel es
un dato incompleto y la ABM lo exige). Sin FX y sin A3500 → None; un FX `0` cuenta como
ausente y cae al A3500. Vencido (`maturity_date <= settle`) → `VanillaStrategy`.
`tamar_forecast` mueve sólo el riel TAMAR, como hoy.

## 4. Popup y patas

`apps/web/bond_detail.py`:

- `_VALID_LEGS` suma `"DL"`. `_parse_leg_ticker("TMVE8_DL")` → `("TMVE8", "DL")`.
- `_apply_leg(inst, "DL")` (sólo para `is_dual_dl_tamar`; en cualquier otro tipo el sufijo
  es un ticker inexistente, como `_TF` fuera de PURO/DUAL): clona el instrumento como
  `DOLAR_LINKED` con un único flujo `Cashflow(date=vto, amortization=100, interest=0)`, sin
  `floor`/`spread` (y **sin** `cer_base`: `DolarLinkedStrategy` no lo lee). Así publica la
  **TIR en USD** del riel dólar-linked (precio en pesos ÷ mayorista venta contra 100 USD a
  vencimiento) y su V.Téc en pesos (100 × FX), sin código nuevo de pricing. Ese V.Téc es
  exactamente el riel DL de la vista base: las dos vistas hablan la misma escala.
- **La pata `_TAM` se RETIRA para este tipo** (ruling de la revisión final): un clon `PURO`
  precia per-100 **pesos** y el precio del papel viene per-100 **USD**, así que la pata
  quedaría en otra escala que el precio. `_resolve_instrument_and_leg` devuelve `None` para
  `<T>_TAM` sobre un `DUAL_DL_TAMAR` (mismo mecanismo que `_TF` fuera de PURO/DUAL). La TEA
  del max de rieles, que es la que aprobó David, ya la publica la vista base. En los demás
  tipos TAMAR la pata sigue igual.
- `_TAMAR_TYPES` suma `DUAL_DL_TAMAR` (Tir Nominal con m=12). `_cupon_label` devuelve
  `max(TAMAR + {spread}%, dólar-linked)`.

Hoy ningún template emite links de patas (se llega por URL: ver `tests/test_bond_detail_leg_tf.py`).
Para este tipo el popup muestra **un** botón en la cabecera («Riel dólar-linked» →
`<T>_DL`) sólo en la vista base (`meta["legs"] == [("Riel dólar-linked", "<T>_DL")]`; dentro
de una pata no se anidan) y la fila «TC inicial» en la descripción (ajuste al escribir el plan).

## 5. ABM y Novedades

`apps/web/instruments_abm.py`, hoja `TAMAR`:

- `tipo`: opciones `["PURO", "DUAL", "DUAL_CER_TAMAR", "DUAL_DL_TAMAR"]`.
- Campo nuevo `tc_inicial` (number, step 0.0001, help «Sólo DUAL_DL_TAMAR — tipo de cambio
  inicial del prospecto, pesos/USD»). Round-trip form → `raw_fields` → `fx_base` → form.
- El tipo es analítico: `save_instrument`/`save_cashflows` rechazan flujos y exigen la fila
  ancla; el preview (`POST /abm/preview_cashflows`) devuelve la fila ancla como hace con
  DUAL_CER_TAMAR. Sin cambios en esas puertas (se cubre con un test de alta de punta a
  punta).
- Validación en el borde: `DUAL_DL_TAMAR` sin `tc_inicial` > 0 se rechaza con mensaje
  claro (misma regla que un CER dual sin `cer_base`).

`core/infrastructure/byma/universe.py::prefill_for`: un título público cuyo ticker
arranca con `TM`, `TT` o `TX` seguido de **letra** abre la hoja `TAMAR` (hoy cae en la hoja
de ON). No preselecciona `tipo` (es obligatorio: lo elige el operador, porque el prefijo no
distingue PURO de dual). Mantiene `fecha_vencimiento` de la ficha.

## 6. Tests y documentación

Todos con fecha fija (`tests/_clock.py`, `MONITOR_TEST_REF_DATE`), stubs para FX, A3500 y
TAMAR, sin red.

- `tests/test_dual_dl_tamar.py` (strategy):
  - gana el riel DL cuando `100 × FX` supera `fx_base ×` la capitalización TAMAR, y
    viceversa; el payoff es exactamente uno u otro (referencia a mano con TAMAR constante:
    `TEM` de `tamar_tem`, `N_meses` 30/360, × TC inicial).
  - V.Téc = max de rieles al settle; antes de la emisión `100 × fx_base`.
  - un FX `0` es dato ausente: cae al A3500 y, sin él, None.
  - `tir` → `price_from_tir` round-trip ≤ 1e-9 relativo; MD = `años/(1+TIR)^(1/12)`.
  - sin `fx_base` → None; sin FX y sin A3500 → None; fallback al A3500 cuando dolarapi no
    responde; `tamar_forecast` mueve sólo el riel TAMAR.
  - registry: `strategy_for` devuelve la strategy nueva y NO cambia la de PURO/DUAL/
    DUAL_CER_TAMAR/DOLAR_LINKED (pineado por tipo).
  - predicados: `is_dual_dl_tamar` True sólo para el tipo; `is_cer`/`is_dolar_linked` False.
- `tests/test_bond_detail_leg_dl.py`: `TMVE8_DL` da TIR en USD (= la de un DL zero-coupon
  con el mismo precio y FX) y su V.Téc coincide con el riel DL de la vista base (ancla de
  escala), `TMVE8_TAM` → None (pata retirada), `AL30_DL` → None, `_cupon_label`,
  `_nominal_tna` m=12.
- ABM (`tests/test_abm_*`): alta por la hoja TAMAR con `tipo=DUAL_DL_TAMAR` y `tc_inicial`
  → fila ancla, `fx_base` en el dominio, rechazo sin `tc_inicial`, rechazo con flujos;
  prefill de un título público `TM`+letra → hoja TAMAR.
- Equivalencia: `tests/test_pricing_equivalence.py` no cambia (el legacy no conoce el tipo
  y el catálogo de pruebas no lo trae); `test_aud_G_tests_equivalence_guard` sigue igual.
- Docs: `docs/convenciones-financieras.md` (sección «DUAL dólar-linked/TAMAR»: rieles,
  convención TEA, pata `_DL`, sin proyección del dólar) y CLAUDE.md › Pricing (una línea
  junto al invariante del payoff DUAL_CER_TAMAR: el riel DL no lleva lag ni spread).

## 7. Fuera de alcance

- PR17 y cualquier BADLAR: carga por ABM como `PROVINCIAL ARS`; no hay serie BADLAR.
- Proyección del dólar (curva DLR de Matba/Rofex, `fx_forecast` en la calculadora).
- Migrar TB27/BAS26/SFN27 a un tipo propio.
- El valor de `tc_inicial` de TMVE8: lo carga el operador desde el prospecto.
