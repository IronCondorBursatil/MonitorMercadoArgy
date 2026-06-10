"""Completa los ISIN faltantes del seed `data/byma/titulos_final.csv` por ficha técnica
BYMA (por grupo ticker_pesos, las variantes comparten ISIN). Reintenta para no perder
por throttling (el bulk del pipeline deja ~800 sin ISIN). In-place + idempotente.

Uso:  py -3.12 scripts/byma_enrich_seed_isin.py
"""
from __future__ import annotations

import collections
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import truststore  # noqa: E402
truststore.inject_into_ssl()
import requests  # noqa: E402

from config.settings import settings  # noqa: E402

BASE = "https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free"
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Content-Type": "application/json",
     "Token": "dc826d4c2dde7519e882a250359a23a7", "Options": "technical-details",
     "Origin": "https://open.bymadata.com.ar", "Referer": "https://open.bymadata.com.ar/"}
FIELDS = ("codigoIsin", "tipoEspecie", "insType", "emisor")
_MON_ORDER = {"ARS": 0, "MEP": 1, "cable": 2}


def ficha(session, symbol, retries=4):
    for attempt in range(retries + 1):
        try:
            r = session.post(BASE + "/bnown/fichatecnica/especies/general",
                             json={"symbol": symbol}, timeout=25)
            if r.status_code == 200:
                d = (r.json() or {}).get("data")
                return d[0] if d else {}
            # 429/5xx → backoff y reintento (throttling del bulk)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4 * (attempt + 1))
    return {}


def main():
    path = settings.byma_catalog_csv
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig"), delimiter=";"))
    groups = collections.defaultdict(list)
    for r in rows:
        groups[r.get("ticker_pesos") or r["symbol"]].append(r)

    before = sum(1 for r in rows if (r.get("codigoIsin") or "").strip())
    # grupos SIN ningún ISIN → candidatos a ficha (probar cotizantes primero)
    todo = {}
    for g, rs in groups.items():
        if any((r.get("codigoIsin") or "").strip() for r in rs):
            continue
        cot = [r for r in rs if str(r.get("cotiza")).strip().lower() == "true"] or rs
        cands = sorted(cot, key=lambda r: _MON_ORDER.get(r.get("moneda"), 9))
        todo[g] = [r["symbol"] for r in cands][:3]
    print(f"grupos sin ISIN a consultar: {len(todo)} (de {len(groups)})")

    session = requests.Session(); session.headers.update(H)

    def fetch(item):
        g, syms = item
        for sym in syms:
            fi = ficha(session, sym)
            if fi.get("codigoIsin"):
                return g, fi
        return g, {}

    found = {}
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:   # baja concurrencia → evita throttle
        for g, fi in ex.map(fetch, todo.items()):
            if fi.get("codigoIsin"):
                found[g] = fi
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(todo)}")

    for g, fi in found.items():
        for r in groups[g]:
            for c in FIELDS:
                if not (r.get(c) or "").strip() and fi.get(c):
                    r[c] = fi[c]

    # escribir de vuelta (mismas columnas/orden)
    cols = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
        w.writeheader()
        w.writerows(rows)

    after = sum(1 for r in rows if (r.get("codigoIsin") or "").strip())
    print(f"ISIN: {before} → {after} / {len(rows)} especies (+{after - before}; "
          f"grupos completados: {len(found)})")
    print(f"-> {path}")


if __name__ == "__main__":
    main()
