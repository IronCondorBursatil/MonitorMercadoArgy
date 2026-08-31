# Plan de optimizacion — Monitor (2026-08-31)

> Analisis multi-agente (6 frentes + verificacion adversarial, 34 agentes).
> Los items de peso muerto (frontend/, api/, vercel.json) YA se aplicaron en d12547a.

---

# Plan de optimización — Monitor Renta Fija AR

## Estado de partida (re-medido hoy sobre HEAD `51893f7`)

- **Ya aplicado** en `d12547a`: `frontend/` (incluido `node_modules`), `api/index.py` y `vercel.json` están borrados. −92 MB y −1.356 archivos. **No los re-evalúes.** El working tree hoy pesa **16 MB** (era ~126 MB): todos los ahorros de disco que circulaban con números de 85/88 MB están cobrados.
- **`render.yaml` está VIVO**: la verificación adversarial midió que `monitor-mercado-argy.onrender.com/api/health` responde. Hay **dos** targets reales: droplet (systemd + `deploy.sh`) y Render (Docker, **plan free = 512 MB RAM**). Todo cambio a `requirements.txt`/`Dockerfile` pega en Render. **No borres `render.yaml` ni el `Dockerfile`.** Corolario: los ahorros de RSS valen el doble ahí que en el droplet de 4 GB.
- El `ThreadPoolExecutor` de `generate_report.py:120` **sí está en HEAD** (el caveat de "ya está serial commiteado" era falso). El ahorro es real.

## Prioridad (ahorro real / riesgo)

| # | Ítem | Ahorro | Riesgo | Superficie |
|---|------|--------|--------|-----------|
| A1 | `git gc` + caches fuera de OneDrive + 2 archivos del índice | 25 MB, ~2.900 archivos de sync | nulo | 0 líneas de app |
| A2 | `requirements-dev.txt` (dev deps fuera de prod) | −47,5 MB, −1.228 archivos, −14,6 s/deploy ×2 targets | nulo | 2 archivos de deps |
| A3 | `optionlab` con import perezoso | −1,1 s boot, −48,5 MB RSS hasta 1er uso | bajo | 1 archivo |
| A4 | Purgar `_bond_history_cache` post-priming | −37 MB RSS **permanentes** | bajo | 2 archivos |
| A5 | 4 handlers `async` que bloquean el event loop | mata freezes de 8,5 s (peor caso 40-60 s) | bajo | 3 routers |
| A6 | Índice en `_store_lookups._matching` | build FCI 1.825 → ~820 ms (−55%) | bajo | 1 función |
| A7 | `/fci/data`: gzip memoizado + single-flight | −230 ms CPU/visita, 128 ms fuera del loop | bajo | 2 archivos |
| A8 | `_npv`: izar `errstate` + `.sum()` | −20 ms/ciclo de pricing | bajo | 1 función |
| A9 | Sacar el `ThreadPoolExecutor` del motor | −50 ms/ciclo, −121.000 threads/día | bajo | 1 bloque |
| A10 | `.dockerignore` | −6 MB de context + corta la fuga de `data/cartera.json` | nulo | 1 archivo |
| B1 | Descartar opciones vencidas en `build_options` | **correctitud**: saca la serie muerta del default del tab | bajo (toca 1 fixture) | tests |
| B2 | Poda de `price_history` a 420 d | −3,7 MB RAM hoy, techo 12 MB vs ~50 MB a 5 años | bajo (dato irrecuperable local) | settings + loop |
| B3 | `holiday_engine`: imports diferidos | −0,36 a −0,50 s boot, 0 MB | bajo (toca day-count) | suite completa |
| B4 | Purga de `_source`/`_opt_source` en el hub | KB + correctitud del listado ABM | bajo | 2 líneas |
| B5 | Dejar de escribir `volume` en `price_history` | −872 floats/ciclo, −25 líneas | bajo | write-path |

---

## 1. Aplicar ya

### A1 — Higiene de repo y OneDrive (cero código)

```powershell
cd "C:\Users\david\OneDrive\Monitores - Data912"

# 1. Git: 2.535 objetos sueltos = 20,62 MiB (el pack real son 96 KiB). Nunca corrió gc.
git gc --prune=now          # --aggressive es innecesario acá (pack de 96 KiB)
git config gc.auto 500

# 2. Caches regenerables: 4,6 MB y ~400 archivos que se reescriben en cada pytest/ruff
Remove-Item -Recurse -Force .hypothesis, .pytest_cache, .ruff_cache, .playwright-mcp -ErrorAction SilentlyContinue
Get-ChildItem -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force

# 3. Dos archivos que no deberían estar versionados
git rm --cached data/history/cafci_diario.json     # 4,0 MB de cache diario (stale: 12-jun)
git rm data/iamc_ref_2026_08_28.json               # duplicado exacto de data/iamc/ref_2026_08_28.json
```

`.gitignore` — agregar:
```
data/history/cafci_diario.json
.hypothesis/
```

`scripts/check.ps1` y `run.bat` — agregar arriba (el equivalente de sacar las `.db` de OneDrive, invariante ya vigente):
```powershell
$env:PYTHONPYCACHEPREFIX = "$env:LOCALAPPDATA\monitor\cache\pycache"
$env:RUFF_CACHE_DIR      = "$env:LOCALAPPDATA\monitor\cache\ruff"
```

`pyproject.toml` — `[tool.pytest.ini_options]` con `cache_dir = "~/AppData/Local/monitor/cache/pytest"`. Para hypothesis: si `HYPOTHESIS_STORAGE_DIRECTORY` no lo toma en 6.155, registrá un profile en `tests/conftest.py` con `database=DirectoryBasedExampleDatabase(...)` apuntando a `%LOCALAPPDATA%\monitor\cache\hypothesis`.

> El grueso del `__pycache__` (4,5 de 4,6 MB, 321 `.pyc`) está en `tests/`: es exactamente el churn que OneDrive re-sincroniza en cada corrida del gate.

**Ojo con `cafci_diario.json`**: al hacer `git pull` en el droplet y en Render, git **borra** el archivo del working tree. Antes de pushear, copiá el archivo al droplet a mano (queda untracked y persiste); en Render se regenera con el primer fetch de CAFCI. Verificá `/fci` después del deploy — si el fetch falla, el panel queda vacío hasta el próximo intento.

### A2 — Sacar pytest/hypothesis/ruff de producción

Crear `requirements-dev.txt`:
```
# Dev only — NO se instala en prod (Dockerfile/Render ni deploy.sh).
pytest==9.0.3
hypothesis==6.155.2
ruff==0.15.16
```
Borrar el bloque `# --- dev ---` de `requirements.txt:38-41` **y** de `requirements.lock:47-50`. `deploy.sh` y `Dockerfile` **no se tocan**: dejan de traerlos solos.

Actualizar la sección "Cómo correr" de `CLAUDE.md` y el README:
```powershell
& $py -m pip install -r requirements.lock -r requirements-dev.txt
```

Verificación: `pwsh scripts/check.ps1` sigue verde local, y en el droplet `pip install -r requirements.txt` deja de bajar 47,5 MB / 1.228 archivos y ahorra 14,6 s por deploy. Nota: **no** cambies `deploy.sh` para que instale el `.lock` — esa propuesta quedó refutada.

### A3 — `optionlab` con import perezoso

`core/domain/options/analytics.py` — reemplazar el `try/except` de nivel módulo (líneas 26-33):

```python
# Import perezoso: optionlab arrastra ~59 MB de RSS y ~0,9 s. Se paga en el primer
# POST /options/analytics (endpoint on-demand, en el threadpool), no en el arranque.
_ol_run_strategy = None
_AVAILABLE: bool | None = None       # None = todavía no se intentó importar


def _load() -> bool:
    global _ol_run_strategy, _AVAILABLE
    if _AVAILABLE is None:
        try:
            from optionlab import run_strategy
            _ol_run_strategy, _AVAILABLE = run_strategy, True
        except ImportError:          # pragma: no cover
            _ol_run_strategy, _AVAILABLE = None, False
            logger.warning("optionlab no instalado; analytics avanzados deshabilitados.")
    return _AVAILABLE
```

Y línea 67: `if not _AVAILABLE:` → `if not _load():`. Único call site: línea 105 (`_ol_run_strategy(inputs)`), que lee el global ya seteado. No hay otros consumidores (grep completo: solo `analytics.py` y `tests/test_options_analytics.py`, que llama `run_analytics` directo).

Enunciado honesto del ahorro: **−1,1 s de boot incondicional y permanente** (3,55 → 2,45 s) + **−48,5 MB de RSS que duran hasta el primer uso del builder** y se recuperan en cada reinicio. Costo trasladado: primer request 1,22 s (vs 0,167 s en caliente). En **Render free (512 MB)** esto es lo más valioso del plan.

Si molesta el primer request: precalentar con `asyncio.to_thread(_load)` en el lifespan después del primer ciclo.

Verificación: `py -3.12 -m pytest tests/test_options_analytics.py -q` (8 passed, transparente al cambio) → `py -3.12 -m ruff check .` → suite completa. Medición:
```powershell
$env:MONITOR_DISABLE_LOOPS=1
py -3.12 -c "import time,psutil;t=time.perf_counter();import apps.web.app;print(time.perf_counter()-t, psutil.Process().memory_info().rss/1048576)"
```

### A4 — Liberar `_bond_history_cache` después del priming (−37 MB permanentes)

`core/infrastructure/data912_provider.py` — agregar:
```python
@classmethod
def clear_history_cache(cls) -> None:
    """Libera el JSON crudo del priming (~37 MB): ya está persistido en el store."""
    with cls._stock_history_lock:
        cls._bond_history_cache.clear()
        cls._stock_history_cache.clear()
```
`apps/web/app.py` — en `_price_history_loop`, justo después de `primed = got > 0` (línea ~259):
```python
if primed:
    provider.clear_history_cache()
```
Alternativa equivalente y más limpia: `fetch_bond_history(..., cache: bool = True)` y que `prime_from_data912` lo llame con `cache=False` (el priming es de un solo uso; el TTL de 6 h no le sirve a nadie).

Sin cambios de comportamiento: nadie más llama `fetch_bond_history`. De paso: `fetch_stock_history` solo se alcanza vía la fachada `provider_hub.py:431`, que **no tiene llamadores** en `apps/` ni `core/` — candidato a borrado en otro frente.

### A5 — Los 4 `async def` que congelan el event loop

Es la clase completa: verificados los 9 `async def` de `apps/web/routers/`, después de esto no queda ninguna corrutina haciendo I/O bloqueante.

1. **`apps/web/routers/bonds.py:102-141` (`cer_drawer_calc`)** — el peor: 8.544 ms medidos de lag del loop; peor caso 40-60 s (2×10 s REM + 4×10 s BCRA bajo timeout). Cambio de 2 líneas, no toca la firma:
```python
data = await asyncio.to_thread(
    cer_projection, ticker, repo, provider, indices, fx,
    price_dirty=price, settlement_lag=lag, bei_sendero=_sendero(state),
    rem_provider=_rem_provider(), custom_infl_monthly=custom_infl,
    custom_monthly=custom_monthly)
```
2. **`apps/web/routers/abm.py:239` (`abm_save`)** y **`:256` (`abm_cashflows`)** — 132-200 ms de freeze por guardado (SQLite + `repo.reload()`). Envolver el bloque `save_* + repo.reload()` en `await asyncio.to_thread(...)`, o pasarlos a `def` leyendo el form con `Form(...)` como ya hace `abm_delete` (`:274`).
3. **`apps/web/routers/panels.py:129` (`save_default_layout`)** — extraer el `os.makedirs`+`open`+`json.dump` a una función sync y `await asyncio.to_thread(_write_layout, obj)`.

### A6 — Índice en `_store_lookups._matching` (FCI −55%)

`apps/web/fci_service.py:22-32`. Hoy es O(fondos × claves): 5,4 M comparaciones × 2 pasadas = 1.012 ms por build.

```python
def _store_lookups(store):
    idx: dict[str, list[str]] = {}
    for k in store.keys():
        idx.setdefault(k, []).append(k)
        b = k.split(" - ", 1)[0]
        if b != k:
            idx.setdefault(b, []).append(k)

    def _matching(fondo):
        return idx.get(norm(fondo), [])
    ...
```
Equivalencia **verificada** sobre los 1.096 fondos reales: resultado ordenado idéntico, 0 diferencias. El índice cuesta 2,1 ms y ocupa 6.270 entradas. Matching: 1.012 ms → 2,46 ms (**205×**).

### A7 — `/fci/data`: gzip memoizado + single-flight

Dos cosas en el mismo toque:

**(a) Bytes gzippeados memoizados** (`apps/web/routers/fci.py:26-28`). Hoy cada visita re-serializa 4,09 MB (92 ms `json.dumps`) y re-comprime (128 ms gzip) aunque el cache sea HIT. Memoizar el `bytes` junto al dict, con la misma key `(generated_at, hoy)`, y devolver:
```python
Response(content=gz, media_type="application/json",
         headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
```
Verificado que **no hay doble compresión**: `starlette/middleware/gzip.py:55` chequea `content-encoding` y se saltea la respuesta. Mantener un fallback sin comprimir para clientes sin `Accept-Encoding: gzip`. El valor principal no es el CPU (0,23 core-s por carga de página): saca **128 ms de gzip de adentro del event loop** (el `GZipMiddleware` comprime en el loop y hoy frena el SSE y el resto de los paneles en cada carga de `/fci`). Costo: ~0,8 MB de RAM.

**(b) Single-flight** en `apps/web/fci_service.py:48-75`: el double-checked locking no impide el stampede. Con N=2-3 (lo realista acá: 1 usuario, 1 worker, `fci.js:660` hace un solo fetch por page load) mide 5-10 GETs redundantes a ArgentinaDatos y 1-2 builds de 1,6 s de CPU evitados; wall del evento 4,00 → 1,92 s, y evita un pico transitorio de 9-18 MB de datasets duplicados. Se dispara en cache frío (restart, primera carga del día, cambio de corte) cuando el usuario recarga impaciente. **Alternativa mínima y suficiente**: computar todo dentro de `_LOCK` — el build es idempotente, serializa pero no multiplica red ni CPU, y con A6 el hold baja a ~800 ms.

Verificación: `py -3.12 -m pytest tests/test_fci_router.py -q`; después dos `curl -H 'Accept-Encoding: gzip' -o /dev/null -w '%{time_total} %{size_download}\n' localhost:8000/fci/data` — el segundo baja de ~0,22 s a <0,02 s, y el JSON descomprimido tiene que ser byte-idéntico. Con contadores: `fetch_ard_fci_rows` = 1 y `build_fci_dataset` = 1 con 8 threads.

### A8 — `_npv`: izar el `errstate`, `.sum()` en vez de `np.sum`

`core/domain/xirr.py:41-48`. Se ejecuta 20.922 veces por ciclo.

```python
def _npv(flows, years, rate: float) -> float:
    if rate <= -1.0:
        return 1e18
    return float((flows / (1.0 + rate) ** years).sum())
```
y envolver el **cuerpo** de `_xirr_from_years` (o el `def npv(rate)` interno junto con el fallback de bracket) en un único `with np.errstate(over="ignore", invalid="ignore", divide="ignore")`. **Crítico**: el `errstate` tiene que cubrir también `_bracket_and_solve`, que evalúa `npv` con `hi` hasta ~1,15e18.

Ahorro medido: −20 a −25 ms/ciclo sobre 236-300 ms (~−8% del pricing). El desglose real está invertido respecto de lo que se decía: izar el errstate aporta −7/−9 ms y el `.sum()` −12,6 ms. Sobre el refresh completo (~5,7 s) es ~0,4%: **no lo vas a ver en el log**, se hace porque es gratis.

Verificación: ya se comprobaron **2.576 valores bit-idénticos** con esta variante. Al implementar: `py -3.12 -m pytest tests/test_pricing_equivalence.py tests/ -q`.

### A9 — Sacar el `ThreadPoolExecutor` del motor

`core/use_cases/generate_report.py:120-131` → for serial que arme `results` en el mismo orden (el código actual ya preserva el orden de submit con `[f.result() for f in futures]`, así que la salida no cambia). Borrar `from concurrent.futures import ThreadPoolExecutor` (línea 3) **y** `from config.settings import settings` (línea 12) — verifiqué que queda sin uso: es el único `settings` del archivo, y si lo dejás, ruff F401 pone el gate en rojo.

Medido sobre el código actual (1.106 instrumentos, 25 muestras, orden aleatorizado): **−50 ms/ciclo** (mediana pareada), −74 ms de media. Overhead puro de despacho: 14,7 ms por 1.106 tareas. El GIL no deja que 8 threads CPU-bound den ningún speedup. Threads eliminados: ~121.000/día (8 × ~15.150 ciclos; el período real es `sleep(5s)` + ~0,7 s de trabajo ≈ 5,7 s). Beneficio secundario real: 1 thread CPU-bound rotando el GIL en vez de 8 compitiendo con el event loop de uvicorn.

Dejá `engine_workers` en `config/settings.py:157` por compatibilidad de env (**Render lo setea explícitamente en `render.yaml`: `MONITOR_ENGINE_WORKERS=1`** — o sea que en Render el ahorro ya está cobrado y esto solo mejora el droplet), pero corregí el comentario que afirma que los threads ayudan.

Hacelo **junto o después de A3/A8**. El motor sigue corriendo dentro de `asyncio.to_thread` desde `app.py:84` — eso **no cambia**: el ciclo no debe bloquear el loop.

Verificación: ya validado (etapa D del experimento acumulativo: 0/672 tickers con métrica distinta del baseline con executor de 8). Al implementar: `py -3.12 -m pytest tests/ -q` (`test_generate_report.py` y `test_panels_router.py` ejercen `execute()`) y mirar en el log que `refresh cycle: ... price=Xs` no suba.

### A10 — `.dockerignore`

Post-limpieza el context ya no son 88,7 MB (`frontend/` murió); hoy el árbol entero son 16 MB. El ahorro real es ~6 MB (mayormente `tests/`, que arrastra 4,5 MB de `__pycache__`) **y cortar la fuga de `data/cartera.json`** (tenencias reales) a la imagen de Render.

Agregar a `.dockerignore`:
```
tests/
docs/
scratch/
.worktrees/
worktrees/
venv/
.venv/
.playwright-mcp/
data/cartera.json
data/history/cafci_diario.json
*.db
*.db-wal
*.db-shm
.env.*
*.bak
agents.md
run.bat
setup.bat
```
No hace falta nada de `frontend/`: ya no existe.

---

## 2. Con cuidado (requiere test o decisión)

### B1 — Opciones vencidas (esto es correctitud, no performance) — **el de mayor valor de esta sección**

Hoy el tab Opciones abre por defecto en una serie **vencida**: el clamp de `days_to_expiry` (`core/domain/options/expiry.py:45-48`, `max(1, ...)`) disfraza el vencimiento y anula la única guarda del pipeline (`rates.py:45 t_days > 0`). Medido: TNA basura de 401% a 8.922% contra 46%-182% de las series vivas, y el sort default (`tna_bruta` desc) las pone **arriba** del scanner. Agravante: los códigos de mes se repiten anualmente, así que una cohorte rancia y su homónima viva del año siguiente se mezclan en la misma pestaña y en la misma tabla de strikes. Muerde tras **un solo** ciclo de vencimiento sin reiniciar (≤1 mes, a veces días), no tras cuatro meses.

1. En `build_options` (`core/domain/options/chain.py`), después de resolver `expiry`: `if expiry < today: continue`. **Esta línea sola arregla la correctitud** venga la fila de donde venga. El camino Data912 sin `maturityDate` no se afecta: `resolve_expiry_date` ya rueda el año hacia adelante.
2. Opcional (compra CPU/RAM, no correctitud): purga en el hub. En `_fetch_options_byma` (`provider_hub.py:332`) y `fetch_options_data912` (`:359`), tras el `.update(merged)`, borrar de `_opt_snapshot` **y** `_opt_source` las claves con `opt_expiry < hoy`. 1×/día al rollover alcanza, igual que la purga de `_snap` (`:182-193`).

**Qué verificar antes**: hay que actualizar **un** fixture, `test_build_uses_byma_underlying_for_unmapped_root`, que hardcodea `opt_expiry="2026-08-21"`. Con el parche aplicado la suite quedó verde (1.479 passed). Test rojo→verde nuevo en `tests/test_options_chain.py`: (a) `build_options({vencida, viva}, stk, today=X)` devuelve solo la viva; (b) `months_for(items,'GGAL')[0]` es el mes vivo; (c) test de hub que tras dos ciclos deja `options_snapshot()` sin la clave vencida. Smoke: `/options` con `t_days > 1` en el default y el scanner por TNA desc sin TNAs de 40%+.

Números honestos del lado CPU: **5,37 ms por contrato vencido por ciclo** ⇒ ~1,3 s por cohorte muerta (~250 contratos) en un ciclo de 60 s ≈ 2% de un core. El "8% de un core" asumía 4 meses de uptime continuo. La RAM (~2,5 MB por 1.000 contratos muertos) **no es argumento**.

### B2 — Poda de `price_history` a 420 días

El único read-path corta en 400 d; el store guarda 36% de filas que nadie lee.

1. `PriceHistoryStore.prune(before: date)` = `DELETE FROM price_history WHERE day < ?` + invalidar `_cache`, calcada de `index_history.py:86-93`.
2. Llamarla desde `_price_history_loop` (`apps/web/app.py`, junto al bloque de backup diario) con `date.today() - timedelta(days=settings.price_history_keep_days)`. La setting **no existe todavía** — agregar `price_history_keep_days: int = 420` en `config/settings.py` (cerca de `price_history_db:104`).
3. **Capar el write-path** o la poda se deshace sola: en `prime_from_data912` (`price_history.py:172-195`), filtrar `points` a `>= today - keep_days` antes del `upsert`. El otro priming ya lo hace vía `max_days=400`.

**420, no 400**: 377 d que consume `_hist_bases` + margen para que el loop horario nunca corte por debajo del cutoff que el motor va a pedir en el mismo tick.

Números corregidos (a 420, no a 400): RAM 12,11 → 8,46 MB (**−3,65 MB**), disco ~8,1 → ~5,5 MB tras VACUUM, carga inicial 112 → 82 ms (una vez por proceso, irrelevante). El valor real es el techo: escritura real ~437 filas/día en el máximo observado (mediana 96-148), acreción ~54k filas/año ⇒ sin poda ~50 MB de RAM a 5 años; con poda, tope estable ~127k filas ≈ 12 MB.

**Trade-off explícito, decidilo**: se pierde la cola 2006-2024 de DICP/PARP/CUAP del store local. Sigue disponible upstream en Data912 `/historical/bonds` (subir la constante y re-primar) si algún día querés un chart profundo.

**Qué verificar antes**: test nuevo copiado de `tests/test_index_history.py:36` (sembrar −500 d/−421 d/−419 d/−1 d, prunear, sobreviven los dos últimos). Test de no-regresión del motor: `_hist_bases(ticker, today, provider)` devuelve la **misma tupla** `(px_7d, px_30d, px_90d, px_ytd, px_1y)` con el store podado que con el completo — **incluido el caso YTD del 2 de enero**. Test de que `prime_from_data912` con barras de 2006 no reinserta nada fuera de ventana.

### B3 — `holiday_engine`: imports diferidos

`core/holiday_engine.py:45` (`import pandas_market_calendars as mcal`) → adentro de `_get_byma()` (ya es `lru_cache`, se importa una sola vez). `:47-49` (`openpyxl`) → adentro de las funciones de generación del Excel. Se importa en cadena desde `core/domain/conventions.py:16`.

Ahorro: **360-500 ms de boot** (12,6%, no 15%). Señal limpia: el subárbol `core.holiday_engine` cae de 575/511/522 ms a 19/25/19 ms. **Ahorro de memoria: 0 MB**, es puro diferimiento. Prioridad baja: hacelo solo si querés minimizar el arranque (junto con A3 te deja en ~2,0 s desde 3,55 s).

**Qué verificar antes**: `holiday_engine` alimenta settlement y day-count. Suite **completa** (`test_pricing_equivalence.py` es la red). Medir con `py -3.12 -X importtime -c "import apps.web.app"` filtrando `openpyxl` y `pandas_market_calendars`.

### B4 — Purga de `_source` / `_opt_source` en el hub

`provider_hub.py:186-190`: agregar `self._source.pop(s, None)` dentro del `for s in stale:`. Para `_opt_source`, que la purga de B1 borre la clave de `_opt_snapshot` **y** de `_opt_source` en el mismo paso. Bytes: cientos de KB tras meses de uptime. El valor real es de correctitud en el listado del ABM, y es la única inconsistencia que queda entre las estructuras del hub. Hacelo junto con B1.

### B5 — Dejar de escribir `volume` en `price_history`

Columna write-only: se computa y escribe todos los ciclos y nadie la lee (`price_history.py:64,67-70,106-127,144-148,277-289`). **No dropear la columna** (SQLite no lo hace barato): sacar el parámetro `volumes` de `record_closes`, el 4to elemento de las tuplas en `_write` y el bloque `volumes` de `record_live_closes` (280-288). La columna queda inerte en el schema — coherente con el invariante forward-only. Ahorro: deja de recorrer y construir un dict de ~872 floats por corte y saca el `COALESCE` del UPSERT; −25 líneas. Si algún día querés el promedio de volumen, se revierte **junto con** un read-path.

---

## 3. No conviene (no volver a evaluar)

- **Sacar `optionlab` / `matplotlib` / el stack Jupyter**: `jupyter>=1.0,<2` es `Requires-Dist` **dura** de optionlab 1.6.0; sacarlo rompe la resolución, y matplotlib es dep real del PDF de ONs.
- **Reemplazar `optionlab` por ~80 líneas de scipy**: reescribir pricing de estrategias para ahorrar disco es riesgo alto por ahorro que A3 ya cobra en RAM.
- **Secante pura-Python en vez de `scipy.optimize.newton`**: la premisa "domina el ciclo" es falsa por ~10× en wall-clock y ~25× en el reclamo de CPU/día.
- **Memoizar el resultado por `(ticker, precio, día, FX)` fuera de rueda**: el mecanismo funciona (100% de hit), pero el ahorro no justifica el riesgo de servir métricas rancias.
- **Cachear `discount_year_fractions` entre `tir` y `duration`**: ahorro ~2× inflado y el mecanismo "limpio" propuesto no es implementable como se describe.
- **Sacar el lock de clase de los 4 providers sync durante la red**: mecanismo y citas ciertos, veredicto refutado — no toques el patrón sin una medición nueva.
- **Fragmentar el re-render del panel de ONs por tick SSE**: la medición es real, el diagnóstico está mal atribuido y la acción rompe el panel en silencio.
- **Tuning de `synchronous`/fsync de SQLite**: no hay riesgo de "database is locked" (`busy_timeout=5000` + WAL); la micro-optimización de fsync no se sostiene por medición.
- **Poda / ventana en `fci_history`**: el diagnóstico de crecimiento es real y está bien medido, pero la acción propuesta está dominada por una alternativa más segura y el riesgo está subestimado.
- **"Bug" de `on_date` en `fci_history` (duplica el corte en días no hábiles)**: el bug existe, la acción y los números propuestos no — necesita rediseño, no un parche.
- **Sacar el cache full-resident de `fci_history`**: la observación es correcta, el reemplazo no se sostiene.
- **Borrar `render.yaml` / `Dockerfile` / "3 targets de deploy muertos"**: **Render está vivo y sirviendo**. Borrarlos rompe producción.
- **Apuntar `deploy.sh` a `requirements.lock`**: los 8 claims de drift son exactos, el cambio quedó refutado. Dejalo en `requirements.txt`.
- **Mover `scratch/` fuera de OneDrive** y **relocalizar `monitores_global.log`** (`config/settings.py:185`): ambas acciones refutadas — la premisa se sostiene, la solución propuesta no. No las toques en este plan.
- **Borrar "módulos huérfanos" en `core/`, `apps/`, `config/`**: barrido completo, **cero** huérfanos. Los 4 candidatos son falsos positivos; borrarlos rompe código vivo.
- **`frontend/`, `api/index.py`, `vercel.json`**: ya borrados en `d12547a`.

---

## Total

| Dimensión | Ahorro |
|---|---|
| **Disco / OneDrive (working tree)** | **~25 MB y ~2.900 archivos** de sync: git gc 20,6 MB / ~2.490 objetos sueltos; caches 4,6 MB / ~400 archivos; `cafci_diario.json` 3,9 MB fuera del índice (frena ~250 KiB/día de crecimiento del pack); `iamc_ref` 20 KB. Elimina la reescritura de 321 `.pyc` en cada `pytest`. |
| **Prod (droplet + Render)** | **−47,5 MB de venv/imagen, −1.228 archivos, −14,6 s por deploy**, ×2 targets. Build context Docker −6 MB y sin fuga de `data/cartera.json`. |
| **RAM residente** | **−41 MB en régimen** (37 del `_bond_history_cache` + 3,7 del store podado) y **−89 MB de pico de arranque** contando los 48,5 MB de `optionlab` hasta el primer `/options/analytics`. En Render free (512 MB) es ~17% del budget. |
| **Arranque** | **−1,5 s** (3,55 → ~2,0 s): 1,1 s de optionlab + 0,4 s de holiday_engine. |
| **Ciclo de pricing** | **−70 ms** (50 del pool + 20 del `_npv`) sobre ~300 ms de cómputo = **−23% del pricing**, ~−1,2% del refresh de 5,7 s. Más **−121.000 threads/día**. |
| **Ciclo de opciones (60 s)** | **−1,3 s por cohorte vencida** y, sobre todo, se deja de mostrar una serie muerta al tope del scanner. |
| **Panel FCI** | Build **1.825 → ~820 ms (−55%)**; **−230 ms de CPU por visita**, de los cuales **128 ms salen del event loop**. |
| **Event loop** | Elimina la clase completa de bloqueos: **8,5 s** medidos (peor caso 40-60 s) en `POST /bond/{t}/cer`, 132-200 ms por guardado del ABM, más el gzip de `/fci`. |

**Orden de ejecución sugerido**: A1 → A2 → A10 (nada de código, cerrá branch y deployá) → A3+A4 (RAM, el mayor impacto en Render) → A5 (latencia percibida) → A6+A7 (FCI) → A8+A9 juntos (misma branch, gate completo) → B1 (correctitud, merece su propia branch con TDD) → B2 → B3/B4/B5 cuando sobre tiempo.

Gate obligatorio antes de cerrar cada branch: `pwsh scripts/check.ps1`.