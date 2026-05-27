# AGENTS.md — Guía para IA / Desarrolladores

**Monitor de instrumentos de renta fija argentinos + Panel Líder + BEI extendido. Documento maestro de arquitectura.**

> ⚠️ **REINGENIERÍA (`mejora.md`) IMPLEMENTADA** — branch `refactor/mejora-reingenieria`.
> La arquitectura **actual** está en **`CLAUDE.md`** (leer primero). Cambios clave:
> - **Pricing core**: la escalera `if _is_*_type()` de `services.py` se reemplazó por
>   Strategy + Protocol + registry (`core/domain/pricing/`); `FinancialEngine` es ahora
>   una fachada. Modelos → Pydantic v2. Equivalencia numérica verificada vs el motor viejo.
> - **Persistencia**: `CatalogRepository` (SQLite, `core/infrastructure/db/`) como drop-in
>   del Excel repo; Excel = semilla (`scripts/ingest_master.py`). DuckDB analytics.
> - **Web**: el `http.server` + SPA (`server.py`, `app.js`, `style.css`, los HTML) fue
>   **retirado** y reemplazado por **FastAPI + HTMX SSR** (`apps/web/app.py` + `routers/` +
>   `templates/`). `run.py` ahora arranca uvicorn. HTTP: `requests`→`httpx`.
> - **Logging** (`config/settings.py::_ConsoleFilter`): la **consola** muestra SOLO lo
>   accionable — `WARNING`+/errores, requests HTTP con status ≥ 400, y todo lo marcado
>   `extra={"console": True}`. El **archivo** `monitores_global.log` recibe **TODO** (INFO,
>   httpx, access; rota 5 MB × 5). Regla para futuros arreglos: **no** ensuciar la consola
>   con INFO por-ciclo (fetches OK, access 2xx) — va al archivo; si querés que un INFO salga
>   puntualmente en la terminal, usá `logger.info(msg, extra={"console": True})`.
> - **Las secciones de abajo sobre la capa web (server.py / app.js / Gridstack / endpoints
>   `/api/*`) son HISTÓRICAS.** Las **convenciones financieras** (CER, TAMAR, BEI, day-counts,
>   MD, accrued, settle T+0/T+1) SIGUEN VIGENTES — el motor preserva la matemática.

---

## VISIÓN GENERAL

Monitor automatizado de los principales segmentos de renta fija en Argentina (Soberanos, Bopreales, Tasa Fija, CER, Dólar Linked, TAMAR PURO, Duales TAMAR, Duales CER/TAMAR, Futuros DLR) + Panel Líder de acciones BYMA, que obtiene precios en tiempo real desde **Data912** (`https://data912.com/live/*` + `https://data912.com/historical/*`), calcula TIR / Duration Modificada / Valor Técnico / Paridad / TNA / TEM de forma centralizada, presenta los resultados en consola, PNG y dashboard web (Gridstack drag-and-drop), e incluye un módulo de **Break-Even Inflation extendido** (NT3/2019 + NT8/2024) con sendero mensual contra REM-BCRA y método de pares. Cada ticker del dashboard abre un **popup de detalle** de 3 tabs (Detalles + Chart + Calculadora pro de bonos) con toggle de liquidación T+0/T+1.

### Stack técnico
- Python 3.12+
- **Precios de mercado**: Data912 live (`arg_notes`, `arg_bonds`, `arg_corp`, `arg_stocks`) + historical (`/historical/bonds`, `/historical/stocks`)
- **Índice CER + TAMAR**: BCRA API v4.0 (vars 30 y 44)
- **Futuros DLR**: Matba/Primary WebSocket público (modo `guest`, sin auth) — reverse-engineered `wss://matbarofex.primary.ventures/ws`
- **FX USD/ARS**: dolarapi.com
- **REM (Relevamiento de Expectativas de Mercado)**: `bcra-rem-api.facujallia.workers.dev`
- **FCI (Fondos Comunes de Inversión)**: CAFCI vía `estadisticas.cafci.org.ar/comparador-de-fondos.json` (catálogo completo + matriz de rendimientos diaria, sin auth)
- **Histórico de precios** (variación intradiaria): `data/history/precio_historico.csv` (TSV)
- **Histórico diario** (popup por ticker): proxy vía data912 con cache TTL 10min
- **Master de instrumentos**: `data/instruments_master.xlsx`
- Matemática: SciPy (Newton + Brentq para XIRR, least_squares para NSS), NumPy, Pandas
- Salida: Tabulate (consola), Matplotlib (PNG), `http.server` (web dashboard) + Chart.js (frontend) + Gridstack (layout)
- HTTP client uniforme: `core/infrastructure/_http.py::http_get_json` (single-shot retry sobre transient errors)
- Tests: pytest (`tests/`); data-quality suite (`scripts/data_quality_check.py`)

---

## LOS 4 PILARES ARQUITECTÓNICOS

| Pilar | Implementación | Regla |
|---|---|---|
| **1. Una config por curva (panel)** | `apps/web/server.py::_build_refresh_context` | Cada curva es un panel del dashboard, declarado como tupla `(id, tipos, opts)`; todos comparten el row-builder `_base_bond_row()`. |
| **2. Excel central como única fuente de instrumentos** | `core/infrastructure/repositories.py::ExcelInstrumentsRepository` | Nadie más lee `instruments_master.xlsx`. Sin listas hardcodeadas. ABM web (en [apps/web/instruments_abm.py](apps/web/instruments_abm.py)) es el único otro escritor permitido (atomic writes vía `.tmp` + `os.replace`). |
| **3. Matemática financiera centralizada** | `core/domain/services.py::FinancialEngine` + `core/domain/cashflow_synth.py` | Única implementación de xirr, TIR, MD, V.Téc, TNA, TEM. Nadie reimplementa fórmulas. Cashflow synthesis es un módulo puro reutilizado por el repo y la ABM. |
| **4. Datos puramente Data912** | `core/infrastructure/repositories.py::Data912MarketDataProvider` | Único provider de precios live + histórico (OHLC). Excepciones documentadas: BCRA (CER, TAMAR), dolarapi (FX), Matba/Primary WS (futuros DLR + spot A3500, sin auth), REM (expectativas IPC), CAFCI (FCI: catálogo + rendimientos diarios). |

---

## PIPELINE

```
instruments_master.xlsx ──► ExcelInstrumentsRepository ──┐
                                                          │
Data912 live ────────────► Data912MarketDataProvider ──┐ │
Data912 historical ────────────────────────────────────┤ │
                                                       ▼ ▼
                                      GenerateMonitorReport.execute(types)
                                                       │
                                                       ▼
                                        FinancialEngine.calculate_tir / duration / technical_value
                                                       │     (usa indices_provider + fx_provider)
                                                       ▼
                            apps/web/server.py ──► dashboard JSON (refresh 5s)
                                                       │
                                                       ▼
                            BEI thread (5min) ──► /api/snapshot bei_tenor/sendero/pares
                            Panel Líder thread ──► /arg_stocks + OHLC histórico (5d/30d + spark)
                            ABM endpoints ───────► CRUD transaccional sobre Excel (openpyxl)
                            Bond history popup ──► /api/bond_history/<TICKER> (cache 10min)
```

---

## ESTRUCTURA DE ARCHIVOS

```
Monitores - Data912/
├── run.py                              # Supervisor: arranca web con auto-restart en crash
├── agents.md                           # Este documento
│
├── apps/
│   ├── cli/monitors/
│   │   ├── _common.py                  # bootstrap: repo Excel cacheado + build_use_case()
│   │   └── bei.py                      # BEI extendido (NT3/2019 + NT8/2024) — compute_bei_tables() para el server
│   └── web/
│       ├── server.py                   # Dashboard JSON + ABM + bond history proxy + supervisor-friendly shutdown
│       ├── instruments_abm.py          # CRUD transaccional sobre instruments_master.xlsx (atomic save)
│       ├── bond_detail.py              # Backend del popup de detalle (3 tabs) + calculadora pro de bonos
│       └── static/
│           ├── index.html              # Gridstack layout drag-and-drop persistido en localStorage
│           ├── app.js                  # SPA: render + curva soberana + popup detalle (3 tabs) + ABM modal + Layout settings
│           ├── style.css               # single source of truth de estilos (cache-busted con ?v=N)
│           ├── curva.html / curva.js   # Página standalone de curva (legacy)
│           └── vendor/gridstack/       # Lib vendored (sin CDN-dep)
│
├── config/
│   ├── settings.py                     # Paths, setup_logging(), .env loader propio
│   └── theme.py                        # Paleta y geometría de los PNG
│
├── core/
│   ├── domain/
│   │   ├── models.py                   # Instrument, Cashflow, MarketSnapshot, InstrumentMetrics
│   │   ├── interfaces.py               # IInstrumentsRepository, IMarketDataProvider
│   │   ├── services.py                 # FinancialEngine (xirr, TIR, MD, V.Téc, TNA, TEM, spreads, dual rails, CER projection)
│   │   ├── instrument_groups.py        # SOBERANOS, BOPREALES, TASA_FIJA, CER, DOLAR_LINKED, TAMAR, DUAL_TAMAR, PANEL_LIDER
│   │   ├── cashflow_synth.py           # Pure cashflow synthesis (compartido entre repo + ABM preview)
│   │   ├── yield_curve.py              # NS, NSS, bootstrapping, Fisher, forward BEI, pair-of-bonds, real_fx_drift
│   │   └── inflation_path.py           # Sendero mensual de BEI (NT8/2024 Fig. 4)
│   ├── infrastructure/
│   │   ├── _http.py                    # http_get_json: session keep-alive + single-shot retry sobre transient (timeout/5xx/429/conn)
│   │   ├── repositories.py             # ExcelInstrumentsRepository + Data912MarketDataProvider (live + OHLC histórico)
│   │   ├── indices_provider.py         # BCRAIndicesProvider (CER + TAMAR, disk-mirrored + offline-friendly)
│   │   ├── fx_provider.py              # DolarAPIProvider (USD/ARS quotes)
│   │   ├── futures_provider.py         # RofexProvider — WS público matba/primary, sin auth, thread daemon persistente
│   │   ├── rem_provider.py             # REMProvider (BCRA expectations API, TTL 6h)
│   │   └── cafci_provider.py           # CAFCIProvider (FCI: catálogo + matriz rendimientos diaria, fetch 1×/día, disk-mirror, offline-friendly)
│   ├── use_cases/
│   │   └── generate_report.py          # GenerateMonitorReport.execute(types) -> [InstrumentMetrics]
│   └── holiday_engine.py               # Calendario BYMA + feriados AR (settlement T+0/T+1)
│
├── data/
│   ├── instruments_master.xlsx         # FUENTE DE VERDAD: 5 hojas (Soberanos, Tasa_Fija, CER, Dolar_Linked, TAMAR)
│   │                                   #                   + Cashflows + Cashflows_Fija
│   └── history/
│       ├── precio_historico.csv        # Histórico TSV para variaciones 7D/30D/1Y (one column per ticker)
│       ├── bei_diario.csv              # Persistencia diaria del BEI (auto-append)
│       ├── cer_diario.csv              # Mirror BCRA var 30 (resilience: opera offline si BCRA cae)
│       ├── tamar_diario.csv            # Mirror BCRA var 44 (idem)
│       ├── a3500_diario.csv            # Mirror BCRA var 5 — tipo de cambio A3500 (usado como spot DLR fuera de rueda)
│       └── cafci_diario.json           # Mirror CAFCI: catálogo + rendimientos FCI del día (resilience: opera offline si CAFCI cae)
│
├── tests/                              # pytest — sin red, sin Excel real
│   ├── conftest.py                     # Inyecta repo root al sys.path
│   ├── test_cashflow_synth.py
│   ├── test_financial_engine.py
│   ├── test_data912_provider.py
│   └── test_instruments_abm.py
│
└── scripts/
    ├── data_quality_check.py           # Suite de resilience: conectividad, latencia, schema, sanity, cache, concurrencia, offline. Exit 0 sólo si CRITICAL pasa.
    └── run_server_test.py              # Smoke-test del web server (arranca, polls /api/snapshot, mata).
```

---

## SCHEMA DE LAS HOJAS DEL EXCEL

| Hoja | Columnas | Notas |
|---|---|---|
| **Soberanos** | ticker, short_name, tipo, fecha_vencimiento, fecha_emision, cupon anual %, frecuencia pagos, base calculo, tipo amortizacion, amort inicio, amort cantidad | BONAR/GLOBAL/BOPREAL. Si no hay cashflows explícitos, `cashflow_synth.synth_cashflows()` los crea. |
| **Tasa_Fija** | ticker, clase, fecha_emision, fecha_pago, tem_licit, precio_fallback | LECAP/BONCAP capitalizable: payoff = 100 × (1+tem_licit)^months (30/360). BONOFIJA usa Cashflows_Fija. |
| **CER** | ticker, tipo, fecha emision, fecha vencimiento, cupon anual %, frecuencia pagos, base calculo, tipo amortizacion, amort inicio, amort cantidad, capital factor, meses cupon, cer emision, categoria | `cer emision` es crítico (ver convenciones). `categoria` es la etiqueta de mercado (ej. "BONCERES CERO CUPON"). |
| **Dolar_Linked** | ticker, fecha_vencimiento, tc_inicial, fecha_emision, cupon anual %, frecuencia pagos, base calculo | Valor par 100 USD. V.Téc en pesos = 100 × mayorista venta. |
| **TAMAR** | ticker, tipo, fecha_emision, fecha_vencimiento, tasa_fija_mensual, spread, cer_base, cer_spread | tipo ∈ {PURO, DUAL, DUAL_CER_TAMAR}. `spread` aplica al rail TAMAR; `cer_spread` solo para DUAL_CER_TAMAR. |
| **Cashflows** | ticker, fecha_pago, amortizacion, cupon_interes | Sobreescribe el generador sintético. Per-100-VN en términos base. |
| **Cashflows_Fija** | ticker, fecha_pago, monto | Para BONOFIJA / pagos únicos. |

---

## CÓMO AGREGAR UNA NUEVA CURVA

1. **Agregar el `instrument_type`** en `instruments_master.xlsx` (hoja correspondiente o crear nueva).
2. **Agregar el tipo** a la constante adecuada en [`core/domain/instrument_groups.py`](core/domain/instrument_groups.py).
3. **Web**: agregar el panel a la tupla `bond_panels` dentro de `_build_refresh_context` en `apps/web/server.py` (y a `all_bond_types` si es nuevo grupo); agregar el schema de columnas en `_get_columns()`; agregar el `{"id": ..., ...}` al `Snapshot.__init__`; agregar el `.grid-stack-item` en `index.html` con `gs-id` matching el monitor id. Las filas se arman con `_base_bond_row()`.
4. **Si requiere matemática nueva**: agregar branch en `FinancialEngine.calculate_tir` / `calculate_technical_value` / `calculate_duration`. Para nuevo tipo de cashflow synth, agregar dispatch en `cashflow_synth.synth_cashflows`.
5. **Si querés el chart curva TIR vs MD**: agregar un `.grid-stack-item.curve-extra` con `data-source="<mid>"` (ver curva_cer, curva_tasa_fija como template); `renderBondCurve()` en `app.js` lo levanta automáticamente.

**Nunca**:
- Hardcodear listas de tickers en un monitor (usar `instrument_groups.py`).
- Crear un cliente HTTP nuevo para precios live (usar `Data912MarketDataProvider`).
- Crear un cliente HTTP nuevo para otra fuente sin pasar por `core/infrastructure/_http.py::http_get_json` (perdés el retry sobre transients).
- Reimplementar TIR / duration / NPV (usar `FinancialEngine`).
- Reimplementar cashflow synthesis (usar `cashflow_synth.synth_cashflows`).
- Leer el Excel maestro fuera de `ExcelInstrumentsRepository` (excepción permitida: `instruments_abm.py` para CRUD vía openpyxl).
- Escribir el Excel sin pasar por `_atomic_save_workbook` (`.tmp` + `os.replace`).

---

## CÓMO AGREGAR UN NUEVO INSTRUMENTO (sin tocar Excel a mano)

Usar el **botón ABM** del dashboard web. Abre un modal con:
- Búsqueda de ticker existente (datalist autocomplete) → carga del row + cashflows.
- "+ Nueva especie" → elige hoja → completa form → preview de cashflows synth en vivo → guarda.
- Tabla de cashflows editable al lado del form: **⟳ Regenerar** llama a `/api/abm/preview_cashflows` (rate limit 120/min global) recalculando desde los fields actuales; **+ Fila** agrega una fila vacía. Al guardar, el endpoint persiste row + cashflows en **una sola transacción atómica** (mismo workbook handle, único save).
- Eliminar por ticker (con confirmación). Borra row del sheet + cashflows huérfanos, todo atómico.

El backend (`/api/abm/instrument`) escribe directo al Excel preservando formato openpyxl. Thread-safe vía RLock global compartido con el loader del repo. Para Soberanos basta llenar los 11 campos (emisión + cupón + freq + amort + ...) y el cashflow se genera solo via `cashflow_synth.synth_cashflows`.

---

## CONVENCIONES CRÍTICAS

### Bonos CER (NT N°8/2024)

#### `CER_BASE` en la hoja de instrumentos

La columna `cer emision` / `cer_emision` debe contener el **CER 10 días hábiles antes de la fecha de emisión**, no el del día de emisión.

Ejemplo del paper (T2X5):
- Fecha de Emisión: 14/03/2023
- Valor a cargar: CER del **28/02/2023** (= emisión − 10 días hábiles BYMA) = **81.22**

#### Cashflows en términos "base"

La hoja `Cashflows` debe almacenar montos per-100-nominal en términos de "base", NO valores nominales-al-pago. El sistema multiplica internamente por `CER_LIQ-10h / CER_BASE` al deflactar el precio. Si los flujos vienen indexados, hay **doble-conteo**.

#### Lag de 10 días hábiles BYMA

`dias habiles previos` / `dias_lag` controla el lag. Default = 10.

### Bonos TAMAR (PURO, DUAL, DUAL_CER_TAMAR)

- **`spread`**: anual decimal sobre TAMAR (ej. 0.05 = TAMAR + 5%). Aplica al rail TAMAR diario: `(1 + (TAMAR_d + spread)/365)`.
- **`tasa_fija_mensual`** (solo DUAL): floor mensual decimal. Bond paga max(TAMAR diario, fixed_daily).
- **`cer_base` + `cer_spread`** (solo DUAL_CER_TAMAR, serie TXMJ*): rail CER independiente. Payoff a vto = max(rail_TAMAR, CER_ratio × (1+cer_spread)^years). Para futuros lejanos, CER se proyecta linealmente desde los últimos 30 días observados (`_project_cer_at`).
- **MD bullet** TAMAR/DUAL usa **m=12** (capitalización mensual) → `MD = years / (1+TEA)^(1/12)`. DL usa m=1.
- **Panel TAMAR** incluye los bonos DUAL re-valuados como si fueran PURO (sufijo `_TAM`) via `FinancialEngine.recompute_as_tamar_puro`.

### Bonos DOLAR LINKED

- Precio en pesos; **par = 100 USD**.
- V.Téc en pesos = `residual_USD × mayorista_venta` (FX desde dolarapi).
- Paridad = `price_pesos / V.Téc_pesos` (en rango 80-105% típico).
- TIR es **USD TIR**: precio se deflacta por FX antes de XIRR.

### Bonos LECAP / BONCAP capitalizables

- Generador sintético usa `tem_licit` + `fecha_emision` + day-count **30/360** (`days_30_360`) para computar `payoff = 100 × (1+tem)^months`. Para S29Y6: con 30/360 → 359 días → 11.97 meses → payoff 132.05 (matchea Balanz). Con `base calculo = "Act/..."` usa days/30 en su lugar.
- V.Téc(t) = `100 × (payoff/100)^(elapsed/total)` (interpolación geométrica desde emisión).
- TIR es TEA pura; TNA y TEM se derivan en base 365 (act/365):
  - `TEM = (1+TEA)^(30/365) − 1`
  - `TNA = 365 × ((1+TEA)^(1/365) − 1)`

### Modified Duration — convención BYMA/IAMC

`MD = Macaulay_years / (1+TEA)^(1/freq)`, donde `freq` es la frecuencia anual de pagos (2 = semestral). El campo `payment_frequency` del Instrument se infiere automáticamente del gap mediano entre cashflows si no está en el Excel.

### Filtro MEP-only para Bonares / Bopreales

En el dashboard web, los paneles `bonares` y `bopreales` muestran **solo tickers MEP** (sufijo `D`). Pesos (sin sufijo) tendrían TIRs negativas absurdas por mismatch ARS-price / USD-cashflows, y CABLE (sufijo `C`) duplicaría puntos con un spread mínimo. Los tickers no-D siguen estando en el master para el popup de histórico, pero no en el panel. (Si en el futuro querés sumar CABLE, ver `_isCurvaTicker` y `_refresh_bond_panels`).

### Date parsing — bug histórico

`_parse_date` detecta strings ISO (`YYYY-MM-DD`) y los parsea con `dayfirst=False`. Sin esto, pandas con `dayfirst=True` swappeaba mes/día en strings ISO (ej. `2026-07-09` → `2026-09-07`). El módulo `cashflow_synth` además normaliza `datetime → date` para evitar TypeError al comparar tipos mixtos (`cf.date >= settle_date`).

### Accrued period para soberanos mid-amortización

Los soberanos mid-amort (AL29D/AL30D/GD30D/...) suelen cargar en Excel **sólo cashflows futuros** (no traen los cupones ya pagados). Antes, el helper `_period_bounds` y el accrued en `calculate_technical_value` caían a `emission_date` como inicio del período corriente cuando no había past flows en Excel — y como emisión puede ser de hace 5+ años, accrued y V.Téc venían completamente inflados (ej. AL29D: 2082 días en vez de 130).

**Regla actual** (`FinancialEngine._period_bounds`):

1. Si hay past flow en Excel → tomar el último.
2. Inferir `prev = next_cf − (12/freq) meses` usando `relativedelta` (aritmética calendario exacta, no `days=365/freq` que daba off-by-1 entre cupones). Si `prev > emission_date`, usar `prev` — es el cupón anterior real.
3. Si `prev ≤ emission_date`, el bond aún no pagó su primer cupón: el período arranca en `emission_date`.

`calculate_technical_value` ahora delega su accrued a `FinancialEngine.accrued_interest` (single source of truth) — no duplica la lógica.

---

## CÓMO EXTENDER LA MATEMÁTICA FINANCIERA

Todo va en [`core/domain/services.py::FinancialEngine`](core/domain/services.py) como `@staticmethod`. Métodos disponibles:

| Método | Devuelve |
|---|---|
| `xirr(flows, dates)` | TIR de un cashflow (decimal fraction; 0.30 = 30%) |
| `calculate_tir(snapshot, indices_provider, fx_provider, settle_date=None)` | TIR del instrumento; branches específicos: CER, DL, TAMAR PURO, DUAL TAMAR, DUAL_CER_TAMAR. `settle_date` override usado por la calculadora del popup (T+0/T+1). |
| `calculate_duration(snapshot, tir, settle_date=None)` | Modified Duration con convención BYMA (`m=freq`); bullets TAMAR/DUAL usan m=12 |
| `calculate_technical_value(snapshot, indices_provider, fx_provider, ref_date=None)` | V.Téc universal: residual + accrued; branches para DL (en pesos), CER (× CER_ratio), TAMAR PURO/DUAL (capitalizado), DUAL_CER_TAMAR (max rails) |
| `calculate_theoretical_price(instrument, tir, ref_date)` | Precio implícito al descontar al TIR dado |
| `tir_from_price(snapshot, price_override, indices, fx, settle_date=None)` | Inversa de `calculate_tir`: TIR para un precio (dirty) dado. Reusa toda la lógica per-type vía `replace` del snapshot. |
| `price_from_tir(snapshot, tir, indices, fx, settle_date=None)` | Precio (dirty) implícito al TIR dado. Branches por tipo: vanilla (PV), CER (real × CER_ratio), DL (USD/FX), TAMAR PURO/DUAL/DUAL_CER_TAMAR (payback / (1+tir)^t). |
| `accrued_interest(instrument, ref_date)` | Intereses corridos per-100-VN, accrual lineal sobre el cupón corriente. 0 para zero-coupon / capitalizables. |
| `days_since_last_coupon(instrument, ref_date)` | Días transcurridos desde el último corte de cupón (o desde emisión si nunca pagó). |
| `residual_nominal(instrument, ref_date)` | Valor Residual per-100 = suma de amortizaciones futuras. Fallback: 100 − amortizado. |
| `current_yield(instrument, price_dirty, ref_date)` | Cupones próximos 12 meses / dirty price (decimal). |
| `dv01(instrument, tir, ref_date)` | ΔP per-100 ante -1bp en TIR. Signo positivo (precio sube cuando yield baja). |
| `convexity(instrument, tir, ref_date)` | Convexidad en años² (PPV en la calculadora del popup). Pareja con MD: `ΔP/P ≈ -MD×Δy + 0.5×C×(Δy)²`. |
| `calculate_pct_change(current, previous)` | Variación porcentual (None-safe + epsilon guard) |
| `tea_to_tem(tea)` | TEA → TEM act/365 |
| `tea_to_tna(tea)` | TEA → TNA base 365 (diaria capitalizada) |
| `recompute_as_tamar_puro(snapshot, indices_provider)` | Re-valúa un DUAL como si fuera PURO (rail TAMAR solo). Usado en panel TAMAR con sufijo `_TAM`. |

### Override de settle_date (T+0/T+1)

Los métodos públicos que dependen del settle date aceptan un parámetro opcional `settle_date` (o `ref_date` para V.Téc) que sobrescribe el default. Sin override, cada método cae a `_settlement_for(instrument_type)` (T+1 para soberanos, T+0 para LECAP/LECER/CI) o `date.today()`. La calculadora del popup usa esto para exponer un toggle T+0/T+1 que recalcula TODO desde la fecha elegida — sin esto, accrued/V.Téc quedaban en T+0 mientras TIR estaba en T+1 (inconsistencia silenciosa de 1 día).

Helper interno: `_resolve_settle(instrument_type, override)` — `override if override is not None else _settlement_for(instrument_type)`.

**Convención T+0 / T+1** en `_settlement_for`:
- **T+0** (Contado Inmediato): LECAP, BONCAP, LECER, cualquier tipo con token "CI"
- **T+1**: todo lo demás (BONAR, GLOBAL, BOPREAL, CER, DOLAR_LINKED, TAMAR PURO, DUAL, DUAL_CER_TAMAR)

### Curvas y BEI ([core/domain/yield_curve.py](core/domain/yield_curve.py))

| Función | Origen |
|---|---|
| `NelsonSiegelCurve` (4 params) | NT8 Eq.11 |
| `NelsonSiegelSvenssonCurve` (6 params) | NT3 Eq.17 |
| `bootstrap_zero_rates(bonds, today)` | NT3 Eq.11-16 |
| `fisher_break_even(i, r)` | NT8 Eq.8 |
| `gamma_known_cer_factor(cer_liq, cer_last)` | NT8 Eq.A4 |
| `forward_rate(curve, t1, t2)` | NT3 Eq.8'/9' |
| `forward_bei_between_tenors(...)` | NT3 Eq.10 |
| `pair_delta(...)` | NT8 Apéndice Eq.A13 (validado: da exactamente 3.81% para S14F5/T2X5) |
| `pair_monthly_inflation(δ, days)` | NT8 Eq.A12 |
| `real_fx_drift(dev_rate, infl_rate)` | Fisher sobre FX |

### Sendero mensual ([core/domain/inflation_path.py](core/domain/inflation_path.py))

`monthly_inflation_path(nom_curve, real_curve, today, months_ahead=12)` — implementa la Fig.4 de NT8/2024. Para cada mes calendario, computa BEI forward usando las curvas y la convención de Fisher por intervalo.

---

## CACHE Y PERFORMANCE

- **Repositorio Excel**: singleton vía [`apps/cli/monitors/_common.py::get_repository`](apps/cli/monitors/_common.py). `_by_type` dict para lookups O(1).
- **Snapshots Data912 live**: cache class-level con TTL 3s — coalesce las ~8 llamadas internas por ciclo de refresh en una sola round-trip a los 3 endpoints (`arg_notes`, `arg_bonds`, `arg_corp`). TTL < `REFRESH_SEC` garantiza data fresca entre ciclos.
- **Stock OHLC histórico** (Panel Líder): cache instance-level por ticker; prefetch paralelo al boot (8 workers) para evitar blow-up del primer ciclo.
- **Bond OHLC histórico** (popup): proxy `_BOND_HISTORY_CACHE` con TTL 600s por ticker. Validación con `HISTORICAL_SUPPORTED_TICKERS` (rechazo temprano con 400 para tickers no soportados upstream — sino se gastan ~4s esperando un 502).
- **Histórico CSV intradiario**: lazy-loaded una vez por instancia. Lookup directo por ticker (headers del CSV = ticker).
- **Índice CER + TAMAR**: `BCRAIndicesProvider` con cache class-level + persistencia en `data/history/{cer,tamar}_diario.csv`. Al startup hidrata desde disco; **un solo intento por día** a BCRA (gate sobre `_last_attempt`, no sobre cache populated — evita thundering herd si BCRA está caída). CER: 30 días. TAMAR: bootstrap 3 años, top-up 30 días. Si BCRA está caído, sigue operando con el último snapshot persistido.
- **FX**: `DolarAPIProvider` TTL 60s, invalidado por el loop cada 30s (cambia lento intraday → refresh cada 5s era overkill).
- **Matba/Primary WS** (futuros DLR + spot): conexión WebSocket persistente en thread daemon — push real-time, sin polling. Snapshot inicial llega en ~500ms-2s del primer connect; updates incrementales después. State dict en memoria, lock thread-safe. Auto-reconnect con backoff exponencial (1s → 60s cap). Heartbeat custom (`ping` string cada 30s). Subscribe topics: `md.rx_DDF_DLR_*` + `md.rx_DDF_BCRA_A3500`. **Sin auth ni credenciales**.
- **REM**: TTL 6h (rate-limit 1 req/min upstream, dataset mensual).
- **CAFCI (FCI)**: `CAFCIProvider` class-level + mirror `data/history/cafci_diario.json`. Un solo fetch exitoso por día (gate sobre `_last_attempt`); en fallo, cooldown de 60s antes de reintentar (evita hammering si el usuario spammea filtros con CAFCI caída). Hidrata de disco al startup + prime en background thread (el primer `/api/fci` no paga el JSON de ~3.9MB). El filtrado/orden/búsqueda se hace en memoria sobre el dataset cacheado. Offline-friendly: sirve el último snapshot persistido.
- **BEI history (/api/bei_history)**: cache por mtime — re-serializa solo si el CSV cambió. Lock granular: I/O y parsing **fuera** del lock (sino bloquea otros endpoints concurrentes).
- **BEI compute**: thread dedicado, **eager** al startup, refresh cada 5 min.
- **Bond panels web**: una sola llamada batched `use_case.execute(all_bond_types)` por ciclo → agrupado por `instrument_type` → distribuido a cada panel. Refresh cada 5s. Adaptive sleep: si el ciclo supera 2× budget, skip sleep + log WARN.
- **Cycle stats**: heartbeat por ciclo a DEBUG + summary a INFO cada 60s (`avg / max / overruns`).
- **Rate limit `/api/abm/preview_cashflows` + `/api/bond_calculate`**: token bucket 120 req/min global compartido (deque + lock) — protege CPU del thread pool de clientes maliciosos. Ambos endpoints pueden ser polleados por keystroke del usuario.

---

## DASHBOARD WEB

### Paneles

| ID | Contenido | Sort |
|---|---|---|
| `bonares` | SOBERANOS (sólo MEP) | MD asc |
| `bopreales` | BOPREALES (sólo MEP) | MD asc |
| `curva_soberana` | Chart TIR vs MD de bonares + bopreales | — |
| `cer` | CER (LECER, BONCER, BONCER ZC, CON CUPON, STEP-UP) | MD asc |
| `tasa_fija` | LECAP / BONCAP / BONOFIJA con TIR + TNA + TEM | MD asc |
| `fci` | FCI (CAFCI): filtros tipo de renta + moneda + buscador, toggle TNA/Directo, períodos 7d/1m/3m/6m/YTD/12m + VCP. Click → popup detalle. Datos diarios (no live) → fetch propio fuera del snapshot de 5s. | TNA del período elegido (headers ordenables) |
| `dolar_linked` | DL con V.Téc en pesos | MD asc |
| `tamar` | TAMAR PURO + DUAL bonds re-valuados como TAMAR puro (sufijo `_TAM`) | MD asc |
| `dual_tamar` | DUAL TAMAR (con floor) + DUAL CER/TAMAR (TXMJ*) | MD asc |
| `futuros` | DLR curve via WS público de Matba; TNA = (futuro_last/`DLR/SPOT`)^(365/d) − 1; spot = índice BCRA A3500 (`Dólar USA` en la UI de Primary) | — |
| `panel_lider` | Acciones BYMA: mid + Día% (data912) + 5d% + 30d% + sparkline 30d | — |
| `bei_tenor` | BEI por tenor estándar (3M-3Y) con TAMAR fwd, BEI γ-adj, BEI TAMAR, Deval DLR, TC real | — |
| `bei_sendero` | Sendero mensual BEI vs REM-BCRA (12 meses) | calendario |
| `bei_pares` | Cross-check método de pares LECAP/CER | — |
| `valor_relativo` | Ranking rich/cheap transversal: junta el `spread_curva` de todos los paneles; más baratos (spread +) arriba, más caros (−) abajo | spread desc |
| `escenarios` | Stress interactivo: sliders Δtasa (bps) + ΔFX (%) → ΔP por bono + P&L de cartera. Fetch propio (POST `/api/scenario`), no vive del snapshot de 5s | ΔP ARS asc |
| `bei_history` (chart) | Evolución diaria de BEI spot por tenor desde CSV | — |
| `curva_<x>` (extras) | Curvas TIR vs MD por panel de bonos (cer, tasa_fija, dolar_linked, tamar, dual_tamar). Ocultas por default; toggle desde **⚙ Layout** | — |

### Layout (Gridstack)

- El grid usa **Gridstack** vendored en `apps/web/static/vendor/gridstack/` (sin CDN-dep).
- Cada panel está envuelto en `.grid-stack-item` con `gs-id` (ancla estable), `gs-w` (ancho 1..60), `gs-h` (alto 1..N, cellHeight = 5px).
- Drag-and-drop desde el header, resize desde la esquina SE.
- El layout se persiste en `localStorage`; el botón **⚙ Layout** abre el settings (visibilidad de paneles + restore default).
- Cache busting: bumpear `?v=N` a las imports de `style.css` / `app.js` / `gridstack` en `index.html` al cambiar CSS/JS.

### Endpoints

| Endpoint | Método | Propósito |
|---|---|---|
| `/` | GET | Sirve `index.html` |
| `/static/*` | GET | Sirve JS/CSS/HTML (incluye `vendor/gridstack/*`) |
| `/api/snapshot` | GET | Snapshot completo: FX + monitors + BEI |
| `/api/bei_history` | GET | Histórico diario de BEI (cached por mtime) |
| `/api/supported_tickers` | GET | Lista de tickers soportados por data912 historical (single source of truth para el frontend) |
| `/api/bond_history/<TICKER>` | GET | Proxy OHLC histórico de data912 con cache 10min. 400 para ticker no soportado, 502 si upstream falla. |
| `/api/fci` | GET | FCI (CAFCI) filtrable: query params `tipo`, `moneda`, `q`, `sort` (período), `dir` (asc\|desc), `metric` (tna\|directo), `limit`. Devuelve `{meta, funds}`. |
| `/api/fci/<clase_id>` | GET | Una clase de FCI por `clase_id` (para el popup de detalle). 404 si no existe. |
| `/api/bond_detail/<TICKER>?lag=0\|1` | GET | Detalle estático para popup: meta + cashflows completos + métricas vivas. `lag` controla T+0/T+1 (default 1). |
| `/api/bond_calculate/<TICKER>` | POST | Recompute para calculadora: `{mode: from_price\|from_tir, price?, price_mode?, tir?, settlement_lag?}`. **Rate limit 120/min** (mismo bucket que `preview_cashflows`). |
| `/api/abm/schemas` | GET | Field metadata por hoja Excel |
| `/api/abm/instruments` | GET | Lista de tickers + hoja |
| `/api/abm/instrument/{ticker}` | GET | Row completo del ticker + cashflows (sheet o synth) |
| `/api/abm/instrument` | POST | Crea/actualiza (upsert por ticker). Acepta `cashflows` para persistir en la misma transacción atómica. |
| `/api/abm/instrument/{ticker}` | DELETE | Elimina row + cashflows huérfanos (atomic) |
| `/api/abm/preview_cashflows` | POST | Sintetiza cashflows desde fields del form sin persistir. **Rate limit 120/min global.** |
| `/api/cartera` | GET | Valuación viva de la cartera: `{positions, summary, cashflows, holdings}`. Lee tenencias de `data/cartera.json` + métricas del snapshot. |
| `/api/cartera` | POST | Upsert tenencia: `{ticker, nominal, cost_price?, note?}`. |
| `/api/cartera/{ticker}` | DELETE | Elimina una tenencia. |
| `/api/scenario` | POST | Stress test: `{d_tir_bps, d_fx_pct}` → ΔP por bono + P&L de cartera. **Rate limit 120/min.** |

### Popup de detalle por bono (3 tabs)

Click en cualquier celda de ticker en cualquier panel abre un popup modal full-size (max 1440px, alto = viewport − 80px overlay padding) con 3 tabs:

| Tab | Contenido | Notas |
|---|---|---|
| **Detalles** | Columna izq: 3 cards (Descripción técnica + Mercado + Métricas vivas). Columna der: tabla Cashflow completa con columnas `Fecha Cupón / VR Cartera / Renta Efect. / Amortización % c/100 VN / Obs. Prox. Pago / Total`. Past cupones en gris, próximo en bold + highlight. | El CF card capa su altura a la natural de la columna izq (JS measure); table-wrap scrollea adentro si los rows exceden. `scrollIntoView` centra el próximo cupón al abrir. |
| **Chart** | Histórico OHLC con range buttons (1M/3M/6M/1Y/All) + selector Pesos/MEP/CABLE. | Reusa `/api/bond_history/<TICKER>`. Tab oculta si data912 no soporta histórico para el ticker (verificado contra `HISTORICAL_SUPPORTED_TICKERS`). |
| **Calculadora** | 4 inputs editables (`Precio Dirty / Precio Clean / TIR / Paridad`). Modificar cualquiera recalcula los otros 3 + 3 cards de resultados (Rentabilidad, Valuación c/100 VN, Sensibilidad). Toggle T+0/T+1 controla el settle date para todas las métricas. | Trigger por ENTER o blur (no debounce-per-keystroke). Conversión Paridad↔Dirty en frontend (usa V.Téc cacheada); TIR↔Precio via backend para preservar lógica per-type. Rate-limited igual que `/api/abm/preview_cashflows` (mismo bucket 120/min). |

Tab transitions usan animación JS de `height` (mide before/after, anima 220ms ease-out) — sin esto el popup snappea entre tabs con altos muy distintos (Detalles largo ↔ Calculadora corta).

Todos los tickers son clickeables (independiente de si tienen OHLC) — la tab Chart se oculta sola cuando no hay histórico, manteniendo Detalles + Calculadora siempre accesibles.

### Resilience del web server

- **Supervisor** en `run.py`: si `web_server_main()` crashea, log + sleep 5s + restart. Ctrl+C exit limpio.
- **Shutdown coordinado** vía `_SHUTDOWN_EVENT` (threading.Event): los daemon threads (refresh + BEI) chequean antes de cada iter y reemplazan `time.sleep` por `Event.wait()` para salir temprano. Sin esto, los threads quedaban mid-cycle en `ThreadPoolExecutor.submit()` durante shutdown y tiraban `RuntimeError("cannot schedule new futures after interpreter shutdown")` al log.
- **`_is_shutdown_error`** detecta esa race y la baja a INFO en vez de stack trace.
- **HTTP handler** atrapa `ConnectionAbortedError/Reset/BrokenPipe` (cliente cierra mid-response al navegar) y lo baja a DEBUG.

---

## ENTORNO DE EJECUCIÓN

**Sin venv en el proyecto.** El proyecto corre directamente con `py -3.12` del sistema (launcher de Windows). No existe ni debe existir ningún directorio `.venv` dentro de la carpeta del proyecto — el OneDrive lo sincronizaría y haría la carpeta pesada. Las dependencias se instalan globalmente con `setup.bat` (`py -3.12 -m pip install -r requirements.txt`). Para correr: `run.bat` o `py -3.12 run.py` desde la terminal.

---

## CONFIG OPCIONAL (.env)

| Variable | Valor | Para qué sirve |
|---|---|---|
| (ninguna) | — | El provider de futuros usa el WS público de Matba/Primary (modo `guest`, sin credenciales). No hay ROFEX_* env vars desde la migración a WebSocket. |

`config/settings.py` tiene su propio `_load_dotenv()` mini-parser — no requiere `python-dotenv`.

REM (`bcra-rem-api.facujallia.workers.dev/api/ipc_general`): sin auth. Rate-limit 1 req/min → `REMProvider` cachea 6h.

---

## TESTING & QUALITY

- **Tests unitarios** (`tests/`): `pytest`. Sin red, sin Excel real (usan tmp + fixtures). Cobertura: cashflow synth, FinancialEngine, instruments_abm, Data912Provider parsing.
- **Suite de data quality**: `python -m scripts.data_quality_check` — chequea conectividad / latencia / schema / value sanity / cache / concurrencia / persistencia para cada feed externo. Exit code 0 sólo si todos los CRITICAL pasan; usa colores ANSI y fuerza UTF-8 en stdout para Windows consoles.
- **Smoke test del server**: `python -m scripts.run_server_test` arranca el web, polls `/api/snapshot`, valida shape básico, mata el proceso.
- **No mocks de la DB/Excel en integration tests** — la suite usa archivos temporales reales (atomic save + roundtrip).

---

## TROUBLESHOOTING

| Error | Causa probable | Solución |
|---|---|---|
| `No se encontró instruments_master.xlsx` | El Excel se movió | Restaurarlo en `data/instruments_master.xlsx` |
| Variaciones 7D/30D/1Y todas `-` para un bono | Su ticker no está como columna en el CSV histórico | Refrescar `data/history/precio_historico.csv` |
| TIR muestra `nan` o números absurdos | Cashflows del Excel desactualizados / vencidos / mal cargados | Revisar hoja `Cashflows`. Para LECAP, verificar `tem_licit` y `fecha_emision` |
| CER/TAMAR no funcionan al primer startup | BCRA API caída + no hay CSV persistido | Verificar `https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias/30` y `/44`. Tras el primer fetch exitoso, `data/history/{cer,tamar}_diario.csv` permiten operar offline. |
| Aparece menos instrumentos de los esperados | Filtro de tipos en `instrument_groups.py` no incluye el tipo del Excel | Agregar el `instrument_type` al grupo correspondiente |
| Paridad DL en miles de % | V.Téc no está usando FX | Verificar que `_enrich_metrics` pase `fx_provider` a `calculate_technical_value` |
| Bono TAMAR PURO con V.Téc=100 fijo | Su `fecha_emision` está en el futuro o falta | Editar hoja TAMAR via ABM |
| BEI panels en "Cargando..." > 60s | El BEI thread está en su primer compute (heavy) | Esperar ~30s; ver logs `BEI thread: computing extended tables...` |
| FUTUROS ROFEX panel vacío / TNA columna toda `–` | WS de Matba aún warm-up (primeros ~2s) o conexión caída | Esperar 2-3s tras el boot; verificar log `Matba WS connected`. Si el WS está caído, el provider re-intenta con backoff exponencial (1s→60s). |
| ImportError `websockets` | Falta la lib | `pip install websockets` (~50KB; única dep del provider de futuros) |
| Popup histórico devuelve 400 para un ticker | No está en `HISTORICAL_SUPPORTED_TICKERS` (data912 no lo soporta) | Validar contra `/api/supported_tickers`; o sumarlo a la constante si data912 lo expone (KEEP IN SYNC con `HISTORY_SUPPORTED_TICKERS` en `app.js`) |
| Excel queda corrupto tras crash del proceso | Antes el wb.save() no era atómico | Ya resuelto: `_atomic_save_workbook` escribe a `.tmp` y `os.replace`. Si pasa, restaurar de `.tmp` o backup |
| `RuntimeError: cannot schedule new futures after interpreter shutdown` al Ctrl+C | Race entre daemon threads y main thread durante shutdown | Ya manejado: `_SHUTDOWN_EVENT` + `_is_shutdown_error`. Si reaparece, chequear que nuevos threads suscriban al Event. |
| Dashboard muestra datos viejos tras edit | Cache del navegador | `Ctrl+Shift+R`. Para changes de JS/CSS/Gridstack: bumpear `?v=N` en `index.html`. |
| Layout perdido tras update | localStorage incompatible con nuevos `gs-id` | Click en **⚙ Layout** → restore default |

---

## CHECKLIST PARA DESARROLLADORES

- [ ] ¿Agregaste un instrumento? Solo en `data/instruments_master.xlsx` (vía ABM web o edición directa).
- [ ] ¿Cambió un flujo? Hoja `Cashflows` (o `Cashflows_Fija`).
- [ ] ¿Nuevo cálculo financiero? `FinancialEngine` en `core/domain/services.py`. Cashflow synth nuevo → dispatch en `core/domain/cashflow_synth.py`.
- [ ] ¿Nuevo monitor CLI? Script en `apps/cli/monitors/`, tipo en `instrument_groups.py`.
- [ ] ¿Nuevo panel web? Schema en `_get_columns`, registro en `Snapshot.__init__`, builder dentro de `_refresh_*` (server.py), `.grid-stack-item` en `index.html` con `gs-id`.
- [ ] ¿Nueva fuente de datos? Justificar por qué no se puede con Data912; si es índice/referencia, modelo análogo a `BCRAIndicesProvider`; usar `core/infrastructure/_http.py::http_get_json` para el cliente HTTP.
- [ ] ¿Cambio de UI? Bumpear `?v=N` en `style.css` / `app.js` / `gridstack` imports de `index.html` para invalidar cache.
- [ ] ¿Nuevo ticker para popup histórico? Agregar a `HISTORICAL_SUPPORTED_TICKERS` en `server.py` Y a `HISTORY_SUPPORTED_TICKERS` en `app.js` (KEEP IN SYNC). Idealmente consumir `/api/supported_tickers` desde el frontend para eliminar el drift.
- [ ] ¿Tests? Cualquier cambio en `cashflow_synth` / `FinancialEngine` / `instruments_abm` / `bond_detail` precisa actualizar/sumar test en `tests/`. Correr `scripts/data_quality_check.py` antes de releases para validar feeds externos.
- [ ] ¿Nuevo método en `FinancialEngine` que dependa de settle date? Aceptar `settle_date` (o `ref_date`) opcional y resolverlo con `_resolve_settle()` para que la calculadora del popup pueda overridearlo en T+0/T+1.

---

**Última actualización:** 2026-05-26
**Versión:** 7.0 — **Reingeniería `mejora.md`** (branch `refactor/mejora-reingenieria`): pricing core Strategy/Protocol/Pydantic (equivalencia verificada), persistencia SQLite+DuckDB (`CatalogRepository`), primitivas async (httpx/breaker/hub), y **web FastAPI + HTMX** reemplazando el http.server + SPA. **Arquitectura actual en `CLAUDE.md`.** · 6.5 — Cartera + Escenarios + Valor Relativo (ver CHANGELOG v6.5). · 6.4 — Panel FCI (CAFCI). Nueva fuente de datos: Fondos Comunes de Inversión vía el micrositio de estadísticas de CAFCI (`estadisticas.cafci.org.ar/comparador-de-fondos.json`), que bundle-a en un solo JSON diario el catálogo completo (1149 fondos / 4602 clases) + la matriz de rendimientos diaria (~3723 clases con VCP + TNA/Directo a 7d/1m/3m/6m/YTD/12m). El método histórico del repo `fedemoglia/cafci-api` (pegarle a `api.cafci.org.ar` sin auth) está muerto: ese host hoy está detrás de una CloudFront Function con allowlist de rutas (`{"error":"Route not allowed"}`). Nuevo `CAFCIProvider` (fetch 1×/día, disk-mirror, offline-friendly, prime en background), endpoints `/api/fci` + `/api/fci/<clase_id>`, panel web `fci` con filtros (tipo de renta + moneda) + buscador + toggle TNA/Directo + headers ordenables + popup de detalle. Tests en `tests/test_cafci_provider.py`.

### CHANGELOG v6.5

Tres features de análisis de portafolio (relevadas de FinceptTerminal pero acotadas a renta fija AR). Dos ya estaban medio construidas y se **generalizaron** en vez de rehacerse.

| Tipo | Item | Detalle |
|---|---|---|
| Feature | **Valor Relativo (rich-cheap)** | El enriquecimiento que vivía solo en `tasa_fija` se generalizó a TODOS los paneles de bonos: `spread_curva` (TIR − ajuste log `a+b·ln(DM)`) + `carry_roll` por panel; `tir_real` solo en pesos nominales (tasa_fija/tamar; CER ya es real, hard-dollar no aplica REM). Nuevo panel `valor_relativo` (ranking transversal: más baratos arriba, más caros abajo). Curvas mono-serie coloreadas por residuo (🟢 barato / 🔴 caro) en `renderBondCurve`. |
| Feature | **Cartera** | Tenencias (ticker + nominal + costo) con valuación viva: P&L, TIR ponderada, MD/convexidad de cartera, exposición por tipo/moneda, calendario de flujos escalado por tenencia. Página `/cartera`. USD→ARS al MEP. |
| Feature | **Escenarios / Stress** | Panel interactivo (sliders Δtasa bps + ΔFX %) → ΔP por bono + P&L de cartera. Reprice analítico MD+convexidad con overlay FX por tipo (β_precio/β_valor: USD→valor ARS, DL→precio ARS, pesos→0). Inflación por sendero mensual sigue en el popup CER (`/api/cer_scenarios`). |
| Engine | `core/domain/portfolio.py` | Agregación pura: `build_portfolio` + `portfolio_cashflows`. Tests en `tests/test_portfolio.py`. |
| Engine | `core/domain/scenarios.py` | Reprice puro: `shock_position` + `portfolio_shock`. Tests en `tests/test_scenarios.py`. |
| Store | `apps/web/cartera_store.py` | Tenencias en `data/cartera.json` (atomic `.tmp`+`os.replace`, RLock propio). NO toca el Excel maestro — las tenencias no son instrumentos. |
| Refactor | `_enrich_curve_metrics` | Extraído de `_enrich_tasa_fija_rows` (que ahora delega + agrega DV01/convexidad). Reusado por todos los paneles. |
| API | `GET/POST/DELETE /api/cartera` + `POST /api/scenario` | Valuación + CRUD de tenencias; stress test (rate-limit 120/min, mismo bucket que preview/calculate). |
| UI/Nav | Header dropdown **"Análisis ▾"** | Agrupa Valor Relativo · Escenarios · Cashflows · BCRA. Nuevo botón **Cartera** (abre `/cartera`). Reemplaza los botones sueltos Cashflows/BCRA. |

**Pendientes v6.5 (para continuar):**
- **Cartera**: P&L solo precio vs costo (falta sumar cupones cobrados desde una `fecha_compra`); calendario de flujos en términos "base" (CER/TAMAR/DL sin proyección de indexación al pago — los hard-dollar USD sí son exactos); el costo USD se convierte al FX actual, no al de compra.
- **Escenarios**: convexidad exacta solo en `tasa_fija` (lineal en el resto → sumar `FinancialEngine.convexity` por bono); el shock de tasa es shift paralelo (falta empinamiento/twist); inflación por sendero sigue solo en el popup CER (`/api/cer_scenarios`).
- **Valor Relativo**: usa ajuste log `a+b·ln(DM)`; para soberanos evaluar NSS (`NelsonSiegelSvenssonCurve.fit`). Falta z-spread / asset-swap.
- **Layout**: los paneles nuevos (`valor_relativo`, `escenarios`) pueden requerir ⚙ Layout → restore default en sesiones con layout viejo en `localStorage`.
- **Tests**: `portfolio` + `scenarios` cubiertos por unit tests (`tests/test_{portfolio,scenarios}.py`); falta integration test HTTP de `/api/cartera` y `/api/scenario` (hoy validados por smoke manual).

### CHANGELOG v6.4

| Tipo | Item | Detalle |
|---|---|---|
| Feature | Panel FCI (CAFCI) | Acceso a TODOS los fondos (10 tipos de renta, 3 monedas) con rendimientos diarios. Default Money Market en pesos por TNA. |
| Source | `CAFCIProvider` | Join catálogo↔matriz sobre `clase_id`, fetch 1×/día con cooldown en fallo, mirror `data/history/cafci_diario.json`, hidratación de disco + prime en background. |
| API | `GET /api/fci` + `GET /api/fci/<clase_id>` | Lista filtrable/ordenable + detalle por clase. |
| UI | Panel `fci` autónomo | No depende del snapshot de 5s — fetch propio on-load + on-filter-change. |

### CHANGELOG v6.1

| Tipo | Item | Detalle |
|---|---|---|
| Feature | Popup de detalle por bono (3 tabs) | Click en cualquier ticker abre Detalles + Chart + Calculadora. Reemplaza al popup de histórico standalone. |
| Feature | Calculadora pro de bonos | 4 inputs editables (Dirty/Clean/TIR/Paridad) con auto-recompute mutuo. Toggle T+0/T+1. Métricas: TIR/TNA/TEM, V.Téc/Residual/Paridad/Accrued, MD/DV01/Convexity/Term. |
| Module | `apps/web/bond_detail.py` | Backend del popup: `get_bond_detail()` + `calculate()`. Resuelve `settle_date` con `_resolve_ref(lag)` (T+1 default, BYMA-aware). |
| API | `GET /api/bond_detail/<TICKER>?lag=0\|1` | Detalle estático + métricas vivas. |
| API | `POST /api/bond_calculate/<TICKER>` | Recompute: `{mode, price?, price_mode?, tir?, settlement_lag?}`. Mismo rate limit que preview_cashflows. |
| FinancialEngine | `+ accrued_interest, days_since_last_coupon, residual_nominal, current_yield, dv01, convexity` | Métricas adicionales para la calculadora. |
| FinancialEngine | `+ tir_from_price, price_from_tir` | Inversas roundtrip-exactas. `price_from_tir` con branches per-type (vanilla/CER/DL/TAMAR/DUAL/DUAL_CER_TAMAR). |
| FinancialEngine | `settle_date` override en `calculate_tir`, `calculate_duration`, `calculate_technical_value`, `tir_from_price`, `price_from_tir` | Permite a la calculadora cambiar T+0/T+1 consistentemente (sin esto TIR y V.Téc quedaban en distintas fechas). |
| Bug fix | Accrued period para soberanos mid-amort | Antes caía a `emission_date` (5+ años atrás) cuando Excel no tenía past flows → AL29D mostraba 2082 días. Ahora infiere `prev = next_cf − 6 meses` con `relativedelta`. Mismo fix aplicado en `calculate_technical_value`. |
| UX | Todos los tickers clickeables | Antes solo `HISTORICAL_SUPPORTED_TICKERS`. Tab Chart se oculta sola si no hay OHLC. |
| UX | Calculadora dispara en ENTER/blur | Antes debounced por keystroke; ahora vos controlás cuándo recalcula. Spinners nativos del `<input type=number>` removidos. |
| UX | CF card capa altura a col izquierda (JS measure) | Sin esto el CF largo (AO27 con 21 rows) inflaba el grid y dejaba empty space gigante en la columna izquierda. |
| UX | Transición animada entre tabs | Mide before/after, anima `height` 220ms ease-out — sin esto el popup snappea cuando Detalles ↔ Calculadora tienen alturas muy distintas. |
