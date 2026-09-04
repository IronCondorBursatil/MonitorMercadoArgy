"""Churn REAL de precios entre dos ciclos del ProviderHub (Fase 0 del plan).

########################################################################
#  CORRELO CON EL MERCADO ABIERTO: 11:00-17:00 ART, dia habil.         #
#  Fuera de rueda TODO da 0% de churn y el numero ENGANA: parece que   #
#  el dirty-check de la Fase 1 ahorra el 100% del CPU cuando lo unico  #
#  que pasa es que no hay operaciones. El script avisa solo si lo      #
#  corres fuera de horario, pero no puede arreglar el dato.            #
########################################################################

HERRAMIENTA MANUAL. NO entra al gate y no tiene tests propios.

    py -3.12 scripts/bench_churn.py               # un par de snapshots
    py -3.12 scripts/bench_churn.py --rounds 5    # 5 pares (mas robusto)
    py -3.12 scripts/bench_churn.py --json        # para guardar la medicion

QUE MIDE Y POR QUE IMPORTA

  El plan asume que la mayoria de los ciclos de 5s reprecia sobre precios
  bit-identicos (la fuente `byma_open` va DEMORADA ~20 min). De ese supuesto
  depende TODO el ROI de la Fase 1 (dirty-check de pricing) y de la Fase 2
  (revision gating), y hoy no esta medido.

  Toma dos snapshots del hub separados por `settings.refresh_sec` y, sobre los
  tickers del catalogo que el hub efectivamente pricea, cuenta:

    * `precio`       -> lo que decide el dirty-check de la Fase 1. Si esto da
                        ~5%, saltear el pricing de los otros ~95% es el ahorro.
    * `cualquier`    -> lo que decide el canal de deltas de las Fases 6/7:
                        `change_pct` y `volume` se muestran en los paneles y se
                        mueven con CADA operacion aunque el precio no cambie
                        (models.py: `InstrumentMetrics` contiene el snapshot).
                        Este numero SIEMPRE es >= el de precio, y la brecha entre
                        los dos es justamente lo que el dirty-check ahorra en CPU
                        pero el canal de deltas igual tiene que transmitir.

  Se reportan los dos por separado a proposito: confundirlos hace que una de las
  dos fases parezca sin sentido.

CAVEAT DEL FLOOR DATA912 (leelo antes de interpretar el numero)

  `ProviderHub` mergea DEBAJO de la fuente activa un floor de Data912 con TTL de
  25s (`_FLOOR_TTL_S`). Los simbolos que solo cubre el floor NO PUEDEN cambiar
  entre dos refreshes separados por 5s: su churn es cero por construccion, no por
  falta de mercado. Para medir tambien esos, usar `--gap 30` (por encima del TTL).
  El default es `settings.refresh_sec` porque ESE es el intervalo real del
  `_refresh_loop`, o sea el churn que veria el dirty-check en produccion.

SOLO LECTURA: el script no escribe nada. Lee la catalog.db (sin `init_db`, ver
`bench_pricing._open_catalog`) para saber que tickers pricear, hace requests HTTP a
la fuente live y compara en memoria. Verifica `settings.db_dir` antes/despues igual
que `bench_pricing`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config.settings import settings  # noqa: E402
# Se reusan del bench hermano en vez de duplicarlos: la apertura read-only del
# catalogo (con el fallback por drift de schema) y el chequeo de "no escribio nada"
# tienen que ser IDENTICOS en los dos scripts o dejan de significar lo mismo.
from scripts.bench_pricing import (  # noqa: E402
    _dir_fingerprint,
    _fingerprint_diff,
    _open_catalog,
)

_AR_TZ = timezone(timedelta(hours=-3))

# Campos del snapshot que el read-path muestra o usa. `price` es el unico que entra
# en el pricing; el resto viaja igual a los paneles.
_FIELDS = ("price", "bid", "ask", "volume", "operations", "change_pct")


class _NoHistory:
    """`history_provider` mudo para `HubMarketDataProvider`: el churn no mira el
    historico y no queremos ni el import ni la red del provider de OHLC."""

    def fetch_historical_prices(self, ticker: str, days: int) -> Dict[Any, float]:
        return {}

    def fetch_stock_history(self, ticker: str):
        return {}


def _market_open_now() -> tuple[bool, str]:
    """(abierto?, descripcion) segun la hora de Buenos Aires. Heuristica de rueda
    (dia habil 11-17), sin calendario de feriados: alcanza para el aviso."""
    now = datetime.now(tz=_AR_TZ)
    stamp = now.strftime("%Y-%m-%d %H:%M ART")
    if now.weekday() >= 5:
        return False, f"{stamp} - FIN DE SEMANA"
    if not (11 <= now.hour < 17):
        return False, f"{stamp} - FUERA DE RUEDA (la rueda es 11:00-17:00)"
    return True, f"{stamp} - rueda abierta"


def _diff_snapshots(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Compara dos dicts {ticker: MarketSnapshot}. Funcion PURA (se testea sola).

    Solo mira los tickers presentes en AMBOS: uno que aparece o desaparece no es
    churn de precio, es un cambio de cobertura, y se reporta aparte."""
    common = sorted(set(a) & set(b))
    per_field = {f: 0 for f in _FIELDS}
    changed_any: List[str] = []
    changed_price: List[str] = []
    for t in common:
        sa, sb = a[t], b[t]
        moved = False
        for f in _FIELDS:
            if getattr(sa, f, None) != getattr(sb, f, None):
                per_field[f] += 1
                moved = True
        if moved:
            changed_any.append(t)
        if getattr(sa, "price", None) != getattr(sb, "price", None):
            changed_price.append(t)
    n = len(common)
    return {
        "common": n,
        "only_in_a": sorted(set(a) - set(b)),
        "only_in_b": sorted(set(b) - set(a)),
        "changed_price": len(changed_price),
        "changed_any": len(changed_any),
        "pct_price": (100.0 * len(changed_price) / n) if n else 0.0,
        "pct_any": (100.0 * len(changed_any) / n) if n else 0.0,
        "per_field": per_field,
        "sample_price": changed_price[:10],
    }


async def _measure(tickers: List[str], rounds: int, gap: float) -> Dict[str, Any]:
    from core.infrastructure.async_http import ResilientClient
    from core.infrastructure.byma.sources import make_source
    from core.infrastructure.provider_hub import HubMarketDataProvider, ProviderHub

    client = ResilientClient()
    try:
        try:
            source = make_source(settings.market_source)
        except Exception as e:  # noqa: BLE001 - byma_realtime sin credenciales, etc.
            print(f"  fuente {settings.market_source} no disponible ({e}); uso byma_open",
                  file=sys.stderr)
            source = make_source("byma_open")
        hub = ProviderHub(client, active_source=source)
        provider = HubMarketDataProvider(hub, _NoHistory())

        await hub.refresh_all()
        prev = provider.fetch_snapshots(tickers)
        results: List[Dict[str, Any]] = []
        for i in range(rounds):
            await asyncio.sleep(gap)
            await hub.refresh_all()
            cur = provider.fetch_snapshots(tickers)
            d = _diff_snapshots(prev, cur)
            d["round"] = i + 1
            results.append(d)
            prev = cur
        return {
            "source_mode": hub.active_mode,
            "source_label": hub.active_label,
            "delayed": hub.is_delayed,
            "hub_symbols": len(hub.snapshot()),
            "rounds": results,
        }
    finally:
        await client.aclose()


def _print_report(meta: Dict[str, Any], data: Dict[str, Any],
                  write_check: Dict[str, Any], out) -> None:
    def p(*a):
        print(*a, file=out)

    rounds = data["rounds"]
    p("")
    p("bench_churn - cuantos precios cambian entre dos ciclos del hub (Fase 0)")
    p("=" * 92)
    if not meta["market_open"]:
        p("  " + "!" * 86)
        p(f"  !! MERCADO CERRADO: {meta['market_clock']}")
        p("  !! Este numero NO sirve para dimensionar la Fase 1: fuera de rueda el churn")
        p("  !! es 0% porque no hay operaciones, no porque la fuente este demorada.")
        p("  !! Repetilo un dia habil entre las 11 y las 17 ART.")
        p("  " + "!" * 86)
    else:
        p(f"  reloj              : {meta['market_clock']}")
    p(f"  fuente live        : {data['source_label']} ({data['source_mode']}"
      f"{', DEMORADA' if data['delayed'] else ''})")
    p(f"  simbolos en el hub : {data['hub_symbols']}")
    p(f"  tickers catalogo   : {meta['tickers']} pedidos, {rounds[0]['common']} priceados")
    p(f"  gap entre snapshots: {meta['gap']}s  (settings.refresh_sec={settings.refresh_sec})")
    if meta["gap"] < 25:
        p("                       ojo: el floor Data912 tiene TTL 25s, asi que los")
        p("                       simbolos que SOLO cubre el floor dan 0% por construccion")
    p(f"  rondas             : {len(rounds)}")
    p("")
    p("  ronda   priceados   precio cambio        cualquier campo cambio")
    p("  " + "-" * 74)
    for r in rounds:
        p(f"  {r['round']:>5}   {r['common']:>9}   {r['changed_price']:>6} "
          f"({r['pct_price']:5.1f}%)          {r['changed_any']:>6} ({r['pct_any']:5.1f}%)")
    if len(rounds) > 1:
        mp = statistics.median(r["pct_price"] for r in rounds)
        ma = statistics.median(r["pct_any"] for r in rounds)
        p("  " + "-" * 74)
        p(f"  mediana          {mp:20.1f}%          {ma:26.1f}%")
    p("")
    p("  campos que se movieron (ultima ronda):")
    for f, n in rounds[-1]["per_field"].items():
        p(f"      {f:<12} {n:>6}")
    if rounds[-1]["sample_price"]:
        p(f"  muestra de tickers con precio nuevo: {', '.join(rounds[-1]['sample_price'])}")
    cov = rounds[-1]["only_in_a"] or rounds[-1]["only_in_b"]
    if cov:
        p(f"  cambio de cobertura (aparecen/desaparecen): {len(cov)} tickers")
    p("")
    p("  LECTURA: 'precio' es lo que ahorra el dirty-check de la Fase 1 (no correr el")
    p("  XIRR). 'cualquier campo' es lo que el canal de deltas de la Fase 6/7 tiene que")
    p("  transmitir igual, porque volume y change_pct se muestran en los paneles.")
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
        p("      (con la app corriendo en paralelo esto es esperable: sus loops")
        p("       escriben price_history/fci_history. El churn en si sigue siendo valido)")
    p("")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Churn real de precios entre ciclos del hub. CORRER CON EL MERCADO ABIERTO.")
    ap.add_argument("--rounds", type=int, default=1,
                    help="pares de snapshots consecutivos (default 1; 5 da una mediana util)")
    ap.add_argument("--gap", type=float, default=None,
                    help=f"segundos entre snapshots (default settings.refresh_sec="
                         f"{settings.refresh_sec}; usar 30 para pasar el TTL del floor)")
    ap.add_argument("--json", action="store_true",
                    help="JSON a stdout (reporte legible a stderr)")
    ap.add_argument("--no-write-check", action="store_true",
                    help="no verificar que db_dir quedo intacto")
    ap.add_argument("--verbose", action="store_true", help="logs de la app en la consola")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.ERROR)
    if not args.verbose:
        logging.disable(logging.WARNING)

    gap = settings.refresh_sec if args.gap is None else args.gap
    if args.rounds < 1:
        print("--rounds tiene que ser >= 1", file=sys.stderr)
        return 2

    open_now, clock = _market_open_now()
    if not open_now:
        print(f"AVISO: {clock}. El churn medido fuera de rueda es 0% y no dimensiona "
              f"nada (ver el docstring).", file=sys.stderr)

    before = None if args.no_write_check else _dir_fingerprint(settings.db_dir)

    repo, catalog_mode, drift, tmpdir = _open_catalog()
    tickers = [i.ticker for i in repo.get_all_instruments()]
    if not tickers:
        print("catalogo vacio: nada que medir.", file=sys.stderr)
        return 2

    data = asyncio.run(_measure(tickers, args.rounds, gap))

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

    meta = {
        "market_open": open_now,
        "market_clock": clock,
        "catalog_mode": catalog_mode,
        "schema_drift": drift,
        "tickers": len(tickers),
        "gap": gap,
        "refresh_sec": settings.refresh_sec,
        "python": sys.version.split()[0],
    }

    if args.json:
        json.dump({"meta": meta, "churn": data, "write_check": write_check},
                  sys.stdout, indent=2, sort_keys=True, default=float)
        sys.stdout.write("\n")
        _print_report(meta, data, write_check, sys.stderr)
    else:
        _print_report(meta, data, write_check, sys.stdout)

    # El write-check NO decide el exit code aca (a diferencia de bench_pricing):
    # este script esta pensado para correr CON la app viva, y sus loops escriben
    # price_history/fci_history por su cuenta. Se reporta, no se falla.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
