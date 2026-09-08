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

- `core/domain/instrument_groups.py`: `DUAL_TAMAR = ["DUAL", "DUAL_CER_TAMAR", "DUAL_DL_TAMAR"]`.
  Con eso el tipo entra solo al panel `tamar` (TAMAR/Dual), a `apps/web/app.py::_ALL_TYPES`,
  a `ANALYTIC_PAYOFF_TYPES` (`has_closed_form_payoff` → fila ancla, sin schedule) y a
  `KNOWN_TYPES`.
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

Definiciones (per 100 de VN, `settle` = fecha de liquidación del contexto):

- **Riel TAMAR** `T(d)` = `tamar_dual_payoff_at(inst, ref, indices, to_date=d,
  tamar_forecast=ctx.tamar_forecast)`. Para este tipo la función devuelve exactamente
  `100 × (1 + TEM_TAMAR+spread)^N_meses` capitalizado desde la emisión: no es `DUAL` (sin
  floor) ni `DUAL_CER_TAMAR` (sin riel CER). **No se modifica** `tamar.py`.
- **FX** = `fx.get_mayorista_venta()` (dolarapi, el mismo que `DolarLinkedStrategy`); si no
  hay dato, `indices.get_a3500(settle)` (fixing BCRA, con forward-fill); si tampoco, None.
- **Riel DL** `DL` = `100 × FX / fx_base`. Es un solo número para el settle: no se proyecta.

Métricas:

| Métrica | Fórmula |
|---|---|
| V.Téc | `max(T(settle), DL)`; antes de la emisión, 100 |
| Payoff proyectado | `P = max(T(vencimiento), DL)` |
| TIR (TEA nominal) | `(P / precio)^(1/años) − 1`, con `años = inst.year_fraction_to(vto, settle)` |
| MD | bullet `años / (1 + TIR)^(1/12)` (m=12, igual que PURO/DUAL/DUAL_CER_TAMAR) |
| `price_from_tir` | `P / (1 + TIR)^años` — inversa exacta: el round-trip cierra por construcción |

Bordes: `fx_base` ausente o ≤ 0 → el riel DL no existe y **no se inventa** (V.Téc y TIR
devuelven None, no el riel TAMAR solo: un dual sin su segundo riel es un dato incompleto y
la ABM lo exige). Sin FX y sin A3500 → None. Vencido (`maturity_date <= settle`) →
`VanillaStrategy`. `tamar_forecast` mueve sólo el riel TAMAR, como hoy.

## 4. Popup y patas

`apps/web/bond_detail.py`:

- `_VALID_LEGS` suma `"DL"`. `_parse_leg_ticker("TMVE8_DL")` → `("TMVE8", "DL")`.
- `_apply_leg(inst, "DL")` (sólo para `is_dual_dl_tamar`; en cualquier otro tipo el sufijo
  es un ticker inexistente, como `_TF` fuera de PURO/DUAL): clona el instrumento como
  `DOLAR_LINKED` con un único flujo `Cashflow(date=vto, amortization=100, interest=0)`,
  `cer_base=1.0`, sin `floor`/`spread`. Así `DolarLinkedStrategy` publica la **TIR en USD**
  del riel dólar-linked (precio en pesos ÷ mayorista venta contra 100 USD a vencimiento) y
  su V.Téc en pesos (100 × FX), sin código nuevo de pricing.
- `_apply_leg(inst, "TAM")` ya clona como `PURO`: habilitado para este tipo, publica la TEA
  del riel TAMAR solo.
- `_TAMAR_TYPES` suma `DUAL_DL_TAMAR` (Tir Nominal con m=12). `_cupon_label` devuelve
  `max(TAMAR + {spread}%, dólar-linked)`.

El template del popup ya ofrece las patas por sufijo; sólo hay que agregar `_DL` donde
lista `_TAM`/`_TF` (mismo mecanismo de toggle).

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
  - gana el riel DL cuando `FX/fx_base` supera la capitalización TAMAR, y viceversa; el
    payoff es exactamente uno u otro (referencia a mano con TAMAR constante: `TEM` de
    `tamar_tem`, `N_meses` 30/360).
  - V.Téc = max de rieles al settle; antes de la emisión 100.
  - `tir` → `price_from_tir` round-trip ≤ 1e-9 relativo; MD = `años/(1+TIR)^(1/12)`.
  - sin `fx_base` → None; sin FX y sin A3500 → None; fallback al A3500 cuando dolarapi no
    responde; `tamar_forecast` mueve sólo el riel TAMAR.
  - registry: `strategy_for` devuelve la strategy nueva y NO cambia la de PURO/DUAL/
    DUAL_CER_TAMAR/DOLAR_LINKED (pineado por tipo).
  - predicados: `is_dual_dl_tamar` True sólo para el tipo; `is_cer`/`is_dolar_linked` False.
- `tests/test_bond_detail_leg_dl.py`: `TMVE8_DL` da TIR en USD (= la de un DL zero-coupon
  con el mismo precio y FX), `TMVE8_TAM` da la TEA del riel TAMAR, `AL30_DL` → None,
  `_cupon_label`, `_nominal_tna` m=12.
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
