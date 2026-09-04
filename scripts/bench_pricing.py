"""Baseline del ciclo de pricing (Fase 0 del plan de optimizacion extrema).

HERRAMIENTA MANUAL. NO entra al gate (`scripts/check.ps1`) y no tiene tests propios:
su correccion se demuestra corriendola. Mide `GenerateMonitorReport.execute` contra
la `catalog.db` REAL (`settings.catalog_db`), con provider de mercado sintetico e
indices hidratados de disco, para poder comparar el mismo numero antes y despues de
las fases 1 (dirty-check), 3 (precomputo diario) y 4 (XIRR cerrado + warm-start).

    py -3.12 scripts/bench_pricing.py                 # reporte legible
    py -3.12 scripts/bench_pricing.py --json > a.json # para diffear entre corridas

SOLO LECTURA - el script no escribe NADA (ni CSVs, ni .db, ni backups):

  * `init_db()` (que en el arranque normal SELLA `schema_meta` y por lo tanto
    ESCRIBE en catalog.db) se neutraliza marcando el engine como ya inicializado.
    El schema de una DB viva ya esta reconciliado; el bench solo hace SELECT.
  * `BCRAIndicesProvider` se hidrata de disco y se le CIERRA el gate diario
    (`_last_attempt = _ar_today()`), lo que inhibe a la vez la red al BCRA y el
    `_save_csv` de los 4 mirrors.
  * El provider de mercado es sintetico y `fetch_historical_prices` devuelve `{}`:
    cero red.
  * El FX es un stub con valores fijos (cero red).
  * Se VERIFICA: se compara el fingerprint (mtime_ns + tamano) de TODO
    `settings.db_dir` antes y despues. Si algo cambio, el bench falla con exit 1
    (`--no-write-check` para saltearlo). Ojo: con el server corriendo en paralelo esa
    comparacion da falso positivo (y ademas los tiempos quedan contaminados) - corre
    el bench con la app apagada.

  Caveat honesto: el fingerprint se toma dentro de `main()`, o sea DESPUES del
  `import config.settings`, que es lo unico que corre antes. Ese import puede crear
  `db_dir` y `db_dir/jwt_secret` si no existen; es un efecto del import de settings
  (comun a cualquier script del repo), no del bench.

PRECIOS SINTETICOS - deterministas y calibrados:

  El multiplicador sale del CRC32 del ticker (estable entre corridas y entre
  procesos, a diferencia de `hash()` de str que esta randomizado por
  PYTHONHASHSEED), pero la ESCALA sale del Valor Tecnico real del instrumento:

      precio = V.Tec * u,   u en [0.60, 1.05] derivado del CRC32

  Un precio "hash puro" en [50,150] seria inservible como baseline: los tramos ARS
  de los soberanos se dividen por el MEP, los CER cotizan en pesos contra un ratio
  CER de ~10x y los DL contra el mayorista - con la escala equivocada la TIR se va a
  miles por ciento y el solver toma el camino lento (bracketing) en casi todos los
  bonos, midiendo un ciclo que no se parece al real. Calibrando por V.Tec la paridad
  cae en 0.60-1.05 para TODOS los tipos, que es el rango de mercado.

  El V.Tec es independiente del precio en todas las strategies (residual + accrued,
  ratio CER, payoff TAMAR capitalizado, residual USD x mayorista), asi que calibrar
  con el motor real no es circular. El tramo ARS de los soberanos se re-escala con
  la MISMA funcion que usa el hot-path (`_sovereign_ars_usd_price`), no con una copia.

  `--price-mode hash` conmuta al precio "hash puro" a proposito, como REGIMEN DE
  STRESS del solver (ver `_synthetic_prices`). Medido en esta maquina sobre el mismo
  catalogo: calibrated ~114 ms y 4.602 `_npv`/ciclo; hash ~421 ms y 21.751
  `_npv`/ciclo. El baseline es `calibrated`.

ESCENARIOS (`--scenarios`): `cold` (primer ciclo del proceso, con los caches frios),
`0` / `10` / `100` = porcentaje de tickers cuyo precio CAMBIA en cada ciclo. El
subconjunto sucio es estable (CRC32 % 1000). Hoy, sin dirty-check, los cuatro
escenarios miden practicamente lo mismo: ESE es el baseline. Despues de la Fase 1 el
escenario `0` deberia bajar >=5x y su contador de `_npv` deberia ir a ~0.

MEDICION: por escenario se corren dos tandas. Primero N ciclos LIMPIOS (sin ningun
wrapper) de los que salen p50/p95/min/max del total. Despues M ciclos INSTRUMENTADOS
que dan el desglose por etapa (TIR / MD / V.Tec) y los contadores de `_npv` y de
llamadas al solver. Estan separados a proposito: envolver `_npv` (miles de llamadas
por ciclo) mete un overhead que arruinaria el total.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
import zlib
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config.settings import settings  # noqa: E402

# FX fijo del stub. Valores del orden del mercado (mayorista ~1.51k) para que la
# calibracion de precios de los DL y de los tramos ARS caiga donde cae en vivo.
# Constantes (no leidas de disco) para que el baseline sea reproducible dia a dia.
FX_MAYORISTA_VENTA = 1515.0
FX_MAYORISTA_MID = 1512.0
FX_MEP_VENTA = 1560.0
FX_CCL_VENTA = 1590.0

DEFAULT_CYCLES = 50
DEFAULT_INSTRUMENTED = 5
DEFAULT_WARMUP = 3


# --------------------------------------------------------------------------- #
# Verificacion de "no escribe nada"
# --------------------------------------------------------------------------- #
def _dir_fingerprint(root: Path) -> Dict[str, Tuple[int, int]]:
    """{path relativo: (mtime_ns, size)} de todos los archivos bajo `root`."""
    out: Dict[str, Tuple[int, int]] = {}
    root = Path(root)
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    return out


def _fingerprint_diff(before: Dict[str, Tuple[int, int]],
                      after: Dict[str, Tuple[int, int]]) -> Dict[str, List[str]]:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(p for p in (set(before) & set(after)) if before[p] != after[p])
    return {"added": added, "removed": removed, "changed": changed}


# --------------------------------------------------------------------------- #
# Provider de mercado sintetico
# --------------------------------------------------------------------------- #
def _crc(ticker: str) -> int:
    """Hash estable entre procesos y corridas (`hash()` de str esta randomizado)."""
    return zlib.crc32(ticker.encode("utf-8"))


class SyntheticMarketProvider:
    """`IMarketDataProvider` sin red. `prices` lo muta el driver entre ciclos.

    Reconstruye los `MarketSnapshot` en cada `fetch_snapshots` igual que
    `HubMarketDataProvider`, para que el costo de materializarlos quede DENTRO
    del ciclo medido (son ~1.150 modelos pydantic por ciclo, no es gratis)."""

    def __init__(self, prices: Dict[str, float]):
        self.prices = prices
        self._today = date.today()
        self.hist_calls = 0

    def fetch_snapshots(self, tickers: List[str]) -> Dict[str, Any]:
        from core.domain.models import MarketSnapshot
        out: Dict[str, Any] = {}
        for t in tickers:
            px = self.prices.get(t)
            if px is None:
                continue
            h = _crc(t)
            out[t] = MarketSnapshot(
                instrument=None, price=px, last_update=self._today,
                bid=px * 0.995, ask=px * 1.005,
                volume=float(h % 1_000_000), operations=h % 500,
                change_pct=((h % 401) - 200) / 100.0,
            )
        return out

    def fetch_historical_prices(self, ticker: str, days: int) -> Dict[date, float]:
        self.hist_calls += 1
        return {}


class StubFx:
    """Stub de FX con los 5 getters que consume el hot-path, todos fijos."""

    _QUOTES = {
        "mayorista": {"compra": FX_MAYORISTA_VENTA - 5.0, "venta": FX_MAYORISTA_VENTA},
        "bolsa": {"compra": FX_MEP_VENTA - 10.0, "venta": FX_MEP_VENTA},
        "contadoconliqui": {"compra": FX_CCL_VENTA - 10.0, "venta": FX_CCL_VENTA},
    }

    def get_quote(self, casa: str) -> Optional[dict]:
        return self._QUOTES.get(casa)

    def get_mayorista_venta(self) -> float:
        return FX_MAYORISTA_VENTA

    def get_mayorista_mid(self) -> float:
        return FX_MAYORISTA_MID

    def get_mep_venta(self) -> float:
        return FX_MEP_VENTA

    def get_ccl_venta(self) -> float:
        return FX_CCL_VENTA


# --------------------------------------------------------------------------- #
# Instrumentacion (solo en la tanda instrumentada)
# --------------------------------------------------------------------------- #
def _patch_module_global(name: str, orig: Any, new: Any) -> List[Any]:
    """Reemplaza el global `name` en TODOS los modulos ya importados donde valga
    `orig`. `_xirr_from_years` esta importado por nombre en `pricing/base.py` y
    `pricing/strategies.py`, asi que parchear solo `core.domain.xirr` no alcanza;
    descubrir los sitios por identidad evita una lista que se desactualiza."""
    sites = []
    for mod in list(sys.modules.values()):
        if mod is None:
            continue
        try:
            if getattr(mod, name, None) is orig:
                setattr(mod, name, new)
                sites.append(mod)
        except Exception:  # noqa: BLE001 - modulos con __getattr__ hostil
            continue
    return sites


class _Instrumentation:
    """Wrappers de medicion: tiempo por etapa (TIR/MD/V.Tec) + contadores del solver.

    Se instala y se desinstala alrededor de la tanda instrumentada; la tanda limpia
    corre con el codigo intacto."""

    STAGES = ("calculate_tir", "calculate_duration", "calculate_technical_value")
    STAGE_KEY = {"calculate_tir": "tir", "calculate_duration": "md",
                 "calculate_technical_value": "vtec"}

    def __init__(self):
        self.stage_s: Dict[str, float] = {"tir": 0.0, "md": 0.0, "vtec": 0.0}
        self.npv_calls = 0
        self.solver_calls = 0
        self._saved_stages: Dict[str, Any] = {}
        self._saved_npv: Any = None
        self._saved_solver: Any = None
        self._npv_sites: List[Any] = []
        self._solver_sites: List[Any] = []

    def reset(self) -> None:
        for k in self.stage_s:
            self.stage_s[k] = 0.0
        self.npv_calls = 0
        self.solver_calls = 0

    def install(self) -> None:
        import core.domain.xirr as xirr_mod
        from core.domain.services import FinancialEngine

        for name in self.STAGES:
            self._saved_stages[name] = vars(FinancialEngine)[name]
            fn = getattr(FinancialEngine, name)
            setattr(FinancialEngine, name,
                    staticmethod(self._timed(self.STAGE_KEY[name], fn)))

        self._saved_npv = xirr_mod._npv
        orig_npv = self._saved_npv

        def counting_npv(flows, years, rate):
            self.npv_calls += 1
            return orig_npv(flows, years, rate)

        self._npv_sites = _patch_module_global("_npv", orig_npv, counting_npv)

        self._saved_solver = xirr_mod._xirr_from_years
        orig_solver = self._saved_solver

        def counting_solver(flows, years, day_count=None):
            self.solver_calls += 1
            return orig_solver(flows, years, day_count)

        self._solver_sites = _patch_module_global(
            "_xirr_from_years", orig_solver, counting_solver)

    def uninstall(self) -> None:
        from core.domain.services import FinancialEngine
        for name, saved in self._saved_stages.items():
            setattr(FinancialEngine, name, saved)
        self._saved_stages.clear()
        for mod in self._npv_sites:
            setattr(mod, "_npv", self._saved_npv)
        for mod in self._solver_sites:
            setattr(mod, "_xirr_from_years", self._saved_solver)
        self._npv_sites, self._solver_sites = [], []

    def _timed(self, key: str, fn):
        def inner(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                self.stage_s[key] += time.perf_counter() - t0
        return inner


# --------------------------------------------------------------------------- #
# Setup: repo + indices + precios calibrados
# --------------------------------------------------------------------------- #
def _open_catalog() -> Tuple[Any, str, Optional[str], Optional[str]]:
    """`CatalogRepository` sobre la catalog.db REAL, sin siembra y sin `init_db`.
    Devuelve `(repo, modo, detalle_drift, tmpdir)`.

    `init_db()` sella `schema_meta` con un INSERT..ON CONFLICT y reconcilia el
    schema con ALTER: en una DB viva es redundante (ya esta reconciliada) pero
    ESCRIBE. Marcar el engine como ya inicializado lo convierte en un no-op y deja
    el bench en solo-lectura.

    Caso borde real: si el ORM va ADELANTE de la DB (una columna nueva todavia sin
    aplicar porque nadie arranco la app desde el ultimo cambio de modelo), saltear
    `init_db` hace que hasta el SELECT falle. Ahi el bench NO escribe la base viva:
    copia la .db (+ -wal/-shm) a un temporal, reconcilia ESA copia y benchea contra
    ella - mismos datos, cero escrituras en `db_dir`. Se avisa en el reporte."""
    import shutil
    import tempfile

    from sqlalchemy.exc import OperationalError

    import core.infrastructure.db.catalog_repository as cr
    from core.infrastructure.db import engine as db_engine

    cr._INITIALIZED_ENGINE = db_engine.get_engine()
    try:
        return cr.CatalogRepository(auto_seed=False), "catalog.db real (solo lectura)", None, None
    except OperationalError as e:
        detail = str(getattr(e, "orig", None) or e).splitlines()[0]

    tmpdir = tempfile.mkdtemp(prefix="bench_pricing_")
    dst = Path(tmpdir) / "catalog.db"
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(settings.catalog_db) + suffix)
        if src.is_file():
            shutil.copy2(src, str(dst) + suffix)
    cr._INITIALIZED_ENGINE = None      # que init_db reconcilie LA COPIA
    db_engine.configure(dst)
    repo = cr.CatalogRepository(auto_seed=False)
    return repo, "COPIA temporal (el ORM va adelante de la DB viva)", detail, tmpdir


def _build_indices():
    """Indices hidratados de disco con el gate diario CERRADO (ni red ni `_save_csv`)."""
    from core.infrastructure.indices_provider import BCRAIndicesProvider, _ar_today
    BCRAIndicesProvider._hydrate_from_disk()
    BCRAIndicesProvider._last_attempt = _ar_today()
    return BCRAIndicesProvider()


def _synthetic_prices(instruments, indices, fx,
                      mode: str = "calibrated") -> Tuple[Dict[str, float], int]:
    """Precios sinteticos deterministas. Dos regimenes, ambos utiles:

    `calibrated` (default): precio = V.Tec * u (u en [0.60,1.05] del CRC32), en la
      moneda de cotizacion. El tramo ARS de un soberano se pricea contra `precio/MEP`
      (o /CCL para GLOBAL), asi que su precio se multiplica por ese mismo divisor -
      obtenido de la funcion del hot-path, no de una copia. V.Tec ausente -> escala 100.
      Paridades 0.60-1.05 = rango de mercado, y el solver converge por Newton en el
      primer seed, que es el camino normal.

    `hash`: precio = 50 + CRC32%10001/100, sin mirar el instrumento. Paridades
      absurdas -> Newton falla los 5 seeds y casi todo cae al bracketing de brentq.
      Medido: ~4,7x mas `_npv` por ciclo y ~3,5x el tiempo total. NO es el baseline
      (no se parece al mercado), pero es el REGIMEN DE STRESS del solver: la Fase 4
      (cerrado mono-flujo + warm-start) tiene que mostrar su ganancia en los dos, y en
      este se ve amplificada."""
    from core.domain.models import MarketSnapshot
    from core.domain.services import FinancialEngine
    from core.use_cases.generate_report import GenerateMonitorReport

    if mode == "hash":
        return ({i.ticker: 50.0 + (_crc(i.ticker) % 10001) / 100.0 for i in instruments}, 0)

    prices: Dict[str, float] = {}
    fallbacks = 0
    for inst in instruments:
        probe = MarketSnapshot(instrument=inst, price=1.0)
        implied = GenerateMonitorReport._sovereign_ars_usd_price(
            inst, probe, FX_MEP_VENTA, FX_CCL_VENTA)
        peso_scale = (1.0 / implied) if implied else 1.0
        try:
            vtec = FinancialEngine.calculate_technical_value(
                probe, indices_provider=indices, fx_provider=fx)
        except Exception:  # noqa: BLE001 - un bono roto no puede tumbar la calibracion
            vtec = None
        if not vtec or vtec <= 0:
            vtec = 100.0
            fallbacks += 1
        u = 0.60 + (_crc(inst.ticker) % 4501) / 10000.0     # 0.60 .. 1.05
        prices[inst.ticker] = vtec * u * peso_scale
    return prices, fallbacks


def _prices_digest(prices: Dict[str, float]) -> str:
    """Huella de los precios sinteticos. Dos corridas del bench sobre el mismo
    catalogo y el mismo dia tienen que dar el MISMO digest; si no, el harness no es
    determinista y comparar p50 entre corridas no significa nada."""
    import hashlib
    h = hashlib.md5()
    for t in sorted(prices):
        h.update(f"{t}:{prices[t]:.6f}|".encode("utf-8"))
    return h.hexdigest()[:16]


def _dirty_set(tickers: Iterable[str], pct: int) -> List[str]:
    """Subconjunto estable con ~`pct`% de los tickers (CRC32 % 1000)."""
    if pct <= 0:
        return []
    if pct >= 100:
        return list(tickers)
    return [t for t in tickers if (_crc(t) % 1000) < pct * 10]


# --------------------------------------------------------------------------- #
# Corrida
# --------------------------------------------------------------------------- #
def _pctl(xs: List[float], q: float) -> float:
    """Percentil por rango mas cercano (sin interpolar: N=50 muestras)."""
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[k]


def _apply_dirty(provider: SyntheticMarketProvider, base: Dict[str, float],
                 dirty: List[str], cycle: int) -> None:
    """Mueve el precio de los tickers sucios con un tick determinista por ciclo."""
    for t in dirty:
        step = ((_crc(t) + cycle) % 7 + 1) * 0.0005
        provider.prices[t] = base[t] * (1.0 + step)


def _run_scenario(use_case_factory, provider, base_prices, dirty_pct, types,
                  cycles, instrumented_cycles, warmup) -> Dict[str, Any]:
    tickers = list(base_prices)
    dirty = _dirty_set(tickers, dirty_pct)

    cycle = 0
    for _ in range(warmup):
        cycle += 1
        _apply_dirty(provider, base_prices, dirty, cycle)
        use_case_factory().execute(types)

    totals: List[float] = []
    n_metrics = 0
    for _ in range(cycles):
        cycle += 1
        _apply_dirty(provider, base_prices, dirty, cycle)
        uc = use_case_factory()
        t0 = time.perf_counter()
        metrics = uc.execute(types)
        totals.append((time.perf_counter() - t0) * 1000.0)
        n_metrics = len(metrics)

    inst = _Instrumentation()
    inst.install()
    stage_ms: Dict[str, List[float]] = {"tir": [], "md": [], "vtec": []}
    inst_totals: List[float] = []
    npv_counts: List[int] = []
    solver_counts: List[int] = []
    try:
        for _ in range(instrumented_cycles):
            cycle += 1
            _apply_dirty(provider, base_prices, dirty, cycle)
            inst.reset()
            uc = use_case_factory()
            t0 = time.perf_counter()
            uc.execute(types)
            inst_totals.append((time.perf_counter() - t0) * 1000.0)
            for k, v in inst.stage_s.items():
                stage_ms[k].append(v * 1000.0)
            npv_counts.append(inst.npv_calls)
            solver_counts.append(inst.solver_calls)
    finally:
        inst.uninstall()

    stages = {k: statistics.median(v) for k, v in stage_ms.items()}
    inst_med = statistics.median(inst_totals) if inst_totals else float("nan")
    stages["resto"] = inst_med - sum(stages.values())
    return {
        "dirty_pct": dirty_pct,
        "dirty_tickers": len(dirty),
        "cycles": len(totals),
        "metrics": n_metrics,
        "p50_ms": statistics.median(totals) if totals else float("nan"),
        "p95_ms": _pctl(totals, 0.95),
        "min_ms": min(totals) if totals else float("nan"),
        "max_ms": max(totals) if totals else float("nan"),
        "mean_ms": statistics.fmean(totals) if totals else float("nan"),
        "instrumented_cycles": len(inst_totals),
        "instrumented_p50_ms": inst_med,
        "stage_p50_ms": stages,
        "npv_calls_median": statistics.median(npv_counts) if npv_counts else 0,
        "solver_calls_median": statistics.median(solver_counts) if solver_counts else 0,
    }


def _run_cold(use_case_factory, types) -> Dict[str, Any]:
    """Primer ciclo del proceso: caches frios (`_HIST_BASE_CACHE` vacio, scipy sin
    warmear, strategies sin resolver). Una sola muestra por definicion."""
    uc = use_case_factory()
    t0 = time.perf_counter()
    metrics = uc.execute(types)
    total = (time.perf_counter() - t0) * 1000.0
    return {"dirty_pct": None, "dirty_tickers": 0, "cycles": 1, "metrics": len(metrics),
            "p50_ms": total, "p95_ms": total, "min_ms": total, "max_ms": total,
            "mean_ms": total}


# --------------------------------------------------------------------------- #
# Reporte
# --------------------------------------------------------------------------- #
def _fmt_row(name: str, r: Dict[str, Any]) -> str:
    st = r.get("stage_p50_ms")
    if st is None:
        tail = "        -        -        -        -  |          -          -"
    else:
        tail = (f"  {st['tir']:7.1f}  {st['md']:7.1f}  {st['vtec']:7.1f}  {st['resto']:7.1f}"
                f"  |  {r['npv_calls_median']:9,.0f}  {r['solver_calls_median']:9,.0f}")
    return (f"  {name:<12} {r['p50_ms']:9.1f} {r['p95_ms']:9.1f} "
            f"{r['min_ms']:8.1f} {r['max_ms']:8.1f}  |{tail}")


def _print_report(meta: Dict[str, Any], results: Dict[str, Dict[str, Any]],
                  write_check: Dict[str, Any], out) -> None:
    def p(*a):
        print(*a, file=out)

    p("")
    p("bench_pricing - baseline del ciclo de pricing (Fase 0)")
    p("=" * 110)
    p(f"  fecha              : {meta['ref_date']}")
    p(f"  catalog.db         : {meta['catalog_db']}")
    p(f"  modo               : {meta['catalog_mode']}")
    if meta.get("schema_drift"):
        p(f"                       drift: {meta['schema_drift']}")
        p("                       (arranca la app una vez y el init_db reconcilia la DB viva)")
    p(f"  instrumentos       : {meta['instruments']} en el catalogo, "
      f"{meta['priced']} con precio sintetico "
      f"({meta['vtec_fallbacks']} sin V.Tec -> escala 100)")
    p(f"  regimen de precios : {meta['price_mode']}")
    p(f"  digest de precios  : {meta['prices_digest']}  "
      f"(igual entre corridas = harness determinista)")
    p(f"  tipos              : {meta['n_types']} (_ALL_TYPES de apps/web/app.py)")
    p(f"  indices (disco)    : CER={meta['idx_cer']} TAMAR={meta['idx_tamar']} "
      f"A3500={meta['idx_a3500']} puntos, gate diario cerrado")
    p(f"  FX (stub fijo)     : mayorista={FX_MAYORISTA_VENTA} "
      f"MEP={FX_MEP_VENTA} CCL={FX_CCL_VENTA}")
    p(f"  ciclos             : {meta['cycles']} limpios + {meta['instrumented_cycles']} "
      f"instrumentados por escenario ({meta['warmup']} de warmup)")
    p(f"  python             : {meta['python']}")
    p("")
    p("  escenario        p50 (ms)  p95 (ms)  min (ms) max (ms)  |"
      "      TIR       MD    V.Tec    resto  |  _npv/ciclo  xirr/ciclo")
    p("  " + "-" * 108)
    for name, r in results.items():
        p(_fmt_row(name, r))
    p("")
    p("  Desglose y contadores salen de la tanda INSTRUMENTADA (mediana), que NO entra")
    p("  en el p50/p95: envolver _npv cuesta y contaminaria el total.")
    a = results.get("0% dirty", {}).get("p50_ms")
    b = results.get("100% dirty", {}).get("p50_ms")
    if a and b:
        p(f"  Ratio 100%/0% dirty: {b / a:.2f}x  (hoy ~1.00x: NO hay dirty-check; "
          f"objetivo Fase 1: >=5x)")
    p("")
    if write_check.get("skipped"):
        p(f"  WRITE-CHECK {settings.db_dir}: SALTEADO (--no-write-check)")
    elif write_check["ok"]:
        p(f"  WRITE-CHECK {settings.db_dir}: OK - no se escribio nada")
    else:
        p(f"  WRITE-CHECK {settings.db_dir}: FALLO")
        for kind in ("added", "removed", "changed"):
            for path in write_check["diff"][kind]:
                p(f"      {kind:<8} {path}")
        p("      (si la app estaba corriendo en paralelo esto es un falso positivo,")
        p("       y ademas los tiempos de arriba estan contaminados: apagala y repeti)")
    p("")


# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Baseline del ciclo de pricing (read-only).")
    ap.add_argument("--cycles", type=int, default=DEFAULT_CYCLES,
                    help=f"ciclos limpios por escenario (default {DEFAULT_CYCLES})")
    ap.add_argument("--instrumented-cycles", type=int, default=DEFAULT_INSTRUMENTED,
                    help=f"ciclos con desglose/contadores (default {DEFAULT_INSTRUMENTED})")
    ap.add_argument("--warmup", type=int, default=DEFAULT_WARMUP,
                    help=f"ciclos descartados antes de medir (default {DEFAULT_WARMUP})")
    ap.add_argument("--price-mode", choices=("calibrated", "hash"), default="calibrated",
                    help="regimen de precios sinteticos (ver _synthetic_prices); "
                         "'hash' es el stress del solver, no el baseline")
    ap.add_argument("--scenarios", default="cold,0,10,100",
                    help="lista separada por comas: cold y/o % de precios sucios")
    ap.add_argument("--json", action="store_true",
                    help="JSON a stdout (reporte legible a stderr) para diffear corridas")
    ap.add_argument("--no-write-check", action="store_true",
                    help="no verificar que db_dir quedo intacto")
    ap.add_argument("--verbose", action="store_true", help="logs de la app en la consola")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.ERROR)
    if not args.verbose:
        logging.disable(logging.WARNING)

    # Fingerprint ANTES de tocar nada (ver el caveat del docstring del modulo).
    before = None if args.no_write_check else _dir_fingerprint(settings.db_dir)

    from apps.web.app import _ALL_TYPES
    from core.use_cases.generate_report import GenerateMonitorReport

    repo, catalog_mode, drift, tmpdir = _open_catalog()
    indices = _build_indices()
    fx = StubFx()
    instruments = repo.get_all_instruments()
    if not instruments:
        print("catalogo vacio: nada que medir.", file=sys.stderr)
        return 2

    base_prices, fallbacks = _synthetic_prices(instruments, indices, fx, args.price_mode)
    provider = SyntheticMarketProvider(dict(base_prices))

    def factory():
        # Se reconstruye por ciclo igual que el refresh loop de apps/web/app.py.
        return GenerateMonitorReport(repo, provider, indices=indices, fx=fx)

    results: Dict[str, Dict[str, Any]] = {}
    for raw in [s.strip() for s in args.scenarios.split(",") if s.strip()]:
        if raw == "cold":
            results["cold"] = _run_cold(factory, _ALL_TYPES)
            continue
        pct = int(raw)
        provider.prices = dict(base_prices)
        results[f"{pct}% dirty"] = _run_scenario(
            factory, provider, base_prices, pct, _ALL_TYPES,
            args.cycles, args.instrumented_cycles, args.warmup)

    from core.infrastructure.indices_provider import BCRAIndicesProvider
    meta = {
        "ref_date": date.today().isoformat(),
        "catalog_db": str(settings.catalog_db),
        "catalog_mode": catalog_mode,
        "schema_drift": drift,
        "instruments": len(instruments),
        "priced": len(base_prices),
        "vtec_fallbacks": fallbacks,
        "price_mode": args.price_mode,
        "prices_digest": _prices_digest(base_prices),
        "n_types": len(_ALL_TYPES),
        "idx_cer": len(BCRAIndicesProvider._cache_cer),
        "idx_tamar": len(BCRAIndicesProvider._cache_tamar),
        "idx_a3500": len(BCRAIndicesProvider._cache_a3500),
        "cycles": args.cycles,
        "instrumented_cycles": args.instrumented_cycles,
        "warmup": args.warmup,
        "python": sys.version.split()[0],
        "hist_provider_calls": provider.hist_calls,
    }

    # El engine mantiene el archivo abierto; cerrarlo antes de mirar el disco (y
    # antes de borrar la copia temporal, si la hubo).
    from core.infrastructure.db import engine as db_engine
    if db_engine._ENGINE is not None:
        db_engine._ENGINE.dispose()
    if tmpdir:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    if before is None:
        write_check: Dict[str, Any] = {
            "ok": True, "skipped": True,
            "diff": {"added": [], "removed": [], "changed": []}}
    else:
        diff = _fingerprint_diff(before, _dir_fingerprint(settings.db_dir))
        write_check = {"ok": not any(diff.values()), "skipped": False, "diff": diff}

    if args.json:
        json.dump({"meta": meta, "scenarios": results, "write_check": write_check},
                  sys.stdout, indent=2, sort_keys=True, default=float)
        sys.stdout.write("\n")
        _print_report(meta, results, write_check, sys.stderr)
    else:
        _print_report(meta, results, write_check, sys.stdout)

    return 0 if write_check["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
