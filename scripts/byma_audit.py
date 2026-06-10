"""Auditoría completa de cotizaciones: catálogo del monitor × cobertura y precio
en BYMA open (delay ~20m), BYMA realtime (si hay clave) y Data912.

Detecta: tickers sin precio en ninguna fuente, divergencias de precio open↔realtime↔
data912, huecos de configuración (ISIN faltante, patas de moneda, bucket/plazo).

Uso:
    py -3.12 scripts/byma_audit.py            # auditoría completa (live)
    py -3.12 scripts/byma_audit.py --no-net   # solo catálogo (sin conexiones)

Escribe un CSV detallado a %LOCALAPPDATA%\\monitor\\byma_audit.csv y un resumen a stdout.
"""

from __future__ import annotations

import asyncio
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Consola Windows: forzar UTF-8 para los símbolos del reporte (cp1252 rompe).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from core.infrastructure.async_http import ResilientClient  # noqa: E402
from core.infrastructure.byma.sources import (  # noqa: E402
    BymaOpenSource, BymaRealtimeSource, Data912Source,
)
from core.infrastructure.db.catalog_repository import CatalogRepository  # noqa: E402


def _mkey(t: str) -> str:
    t = (t or "").upper().strip()
    return t[:-4] if t.endswith("_CER") else t


async def _gather(verbose: bool = True):
    """{source: (rows, smap)} para open/data912/realtime (este último si hay clave)."""
    client = ResilientClient(timeout=20.0)
    out: dict = {}
    try:
        out["open"] = await BymaOpenSource().fetch(client)
        out["data912"] = await Data912Source().fetch(client)
        if BymaRealtimeSource.has_credentials():
            try:
                out["realtime"] = await BymaRealtimeSource().fetch(client)
            except Exception as e:  # noqa: BLE001
                if verbose:
                    print(f"  realtime NO disponible desde este equipo: {type(e).__name__}: {e}")
        else:
            if verbose:
                print("  realtime: sin BYMADATA_USER/PASS → omitido (esperado fuera de rueda/AR)")
    finally:
        await client.aclose()
    return out


def _pct_diff(a, b):
    if not a or not b:
        return None
    try:
        return (float(a) - float(b)) / float(b) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def run(net: bool = True) -> int:
    repo = CatalogRepository()
    insts = repo.get_all_instruments()
    by_ticker = {i.ticker.upper(): i for i in insts}  # ya viene expandido por pata
    tickers = sorted(by_ticker)

    print("=" * 78)
    print(f"AUDITORÍA BYMA — catálogo del monitor: {len(tickers)} especies (tickers)")
    print("=" * 78)

    by_type = Counter(i.instrument_type for i in by_ticker.values())
    print("\nPor tipo de instrumento:")
    for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"  {t or '(sin tipo)':22} {n:5}")

    isin_n = sum(1 for i in by_ticker.values() if getattr(i, "isin", None))
    print(f"\nISIN: {isin_n}/{len(tickers)} especies con ISIN "
          f"({100 * isin_n // max(1, len(tickers))}%)")

    if not net:
        print("\n(--no-net: sin auditar conexiones)")
        return 0

    print("\nConectando a las fuentes (open / data912 / realtime)…")
    sources = asyncio.run(_gather())
    # fetch() devuelve ({"24":{...},"CI":{...}}, smap). Cobertura sobre el plazo 24hs.
    src_rows = {k: v[0].get("24", {}) for k, v in sources.items()}
    src_ci = {k: v[0].get("CI", {}) for k, v in sources.items()}
    src_smap = {k: v[1] for k, v in sources.items()}

    print("\nVolumen por fuente (24hs | CI):")
    for s in ("open", "realtime", "data912"):
        if s in src_rows:
            buckets = dict(Counter(src_smap[s].values()))
            print(f"  {s:9} 24hs={len(src_rows[s]):5}  CI={len(src_ci[s]):5}  buckets={buckets}")

    # --- por ticker del catálogo: cobertura + precio por fuente ---
    rows_out = []
    covered = {s: 0 for s in src_rows}
    missing_all = []
    big_diffs = []
    for tk in tickers:
        inst = by_ticker[tk]
        mk = _mkey(tk)
        rec = {"ticker": tk, "type": inst.instrument_type,
               "category": getattr(inst, "category", None) or "",
               "isin": getattr(inst, "isin", None) or ""}
        prices = {}
        for s, rows in src_rows.items():
            row = rows.get(mk) or rows.get(tk)
            if row is not None:
                covered[s] += 1
                prices[s] = row.c
                rec[f"{s}_price"] = row.c
                rec[f"{s}_bid"] = row.px_bid
                rec[f"{s}_ask"] = row.px_ask
                rec[f"{s}_bucket"] = src_smap[s].get(mk) or src_smap[s].get(tk) or ""
            else:
                rec[f"{s}_price"] = ""
        rec["any"] = bool(prices)
        if not prices:
            missing_all.append(tk)
        # divergencias open↔data912 y open↔realtime
        d_od = _pct_diff(prices.get("open"), prices.get("data912"))
        d_or = _pct_diff(prices.get("open"), prices.get("realtime"))
        rec["diff_open_vs_data912_pct"] = round(d_od, 2) if d_od is not None else ""
        rec["diff_open_vs_realtime_pct"] = round(d_or, 2) if d_or is not None else ""
        if d_od is not None and abs(d_od) > 5:
            big_diffs.append((tk, prices.get("open"), prices.get("data912"), d_od))
        rows_out.append(rec)

    print("\nCobertura del catálogo por fuente:")
    for s, n in covered.items():
        print(f"  {s:9} {n:5}/{len(tickers)}  ({100 * n // max(1, len(tickers))}%)")

    # --- tickers SIN precio en ninguna fuente (problema real de config/listado) ---
    print(f"\n⚠ Sin precio en NINGUNA fuente: {len(missing_all)}")
    miss_by_type = defaultdict(list)
    for tk in missing_all:
        miss_by_type[by_ticker[tk].instrument_type].append(tk)
    for t, tks in sorted(miss_by_type.items()):
        print(f"  [{t or '(sin tipo)'}] ({len(tks)}): {', '.join(tks[:25])}"
              + (" …" if len(tks) > 25 else ""))

    # --- divergencias grandes open vs data912 (>5%) ---
    if "data912" in src_rows:
        print(f"\nDivergencias open↔data912 > 5%: {len(big_diffs)} "
              f"(esperable algo por la demora de 20m de open)")
        for tk, po, pd_, d in sorted(big_diffs, key=lambda x: -abs(x[3]))[:20]:
            print(f"  {tk:8} open={po} data912={pd_} ({d:+.1f}%)")

    # --- CSV detallado ---
    out_csv = settings.db_dir / "byma_audit.csv"
    fields = ["ticker", "type", "category", "isin", "any",
              "open_price", "open_bid", "open_ask", "open_bucket",
              "realtime_price", "realtime_bucket",
              "data912_price", "data912_bucket",
              "diff_open_vs_data912_pct", "diff_open_vs_realtime_pct"]
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows_out)
    print(f"\nCSV detallado → {out_csv}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(run(net="--no-net" not in sys.argv))
