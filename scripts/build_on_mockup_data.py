"""Genera el snapshot REAL de Obligaciones Negociables para los mockups.

One-shot, read-only: pega al endpoint `/on/data` de la app viva (http://localhost:8000),
que YA devuelve el dataset unificado del panel ON (bonos con ticker/emisor/clase/tipo/
sector/ley/vto/precio/paridad/tir/md/%día/vol + resúmenes por sector + meta) y lo escribe
tal cual como `window.ON_DATA` en `docs/mockups/on/_shared/on_data.js`. Los mockups lo
cargan directo (portátil: andan offline, sin servidor).

`/on/data` ya clasifica por sector y trae `clase`/`tipo`, así que NO hace falta re-parsear
HTML ni re-derivar sectores acá (antes esto scrapeaba 3 endpoints SSR y perdía clase/tipo).

Uso:
    py -3.12 scripts/build_on_mockup_data.py            # default http://localhost:8000
    MONITOR_BASE=http://localhost:8000 py -3.12 scripts/build_on_mockup_data.py

NO toca la app ni la DB. Solo HTTP GET + escribe un .js de datos congelados.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.request import urlopen

BASE = os.environ.get("MONITOR_BASE", "http://localhost:8000").rstrip("/")
OUT = Path(__file__).resolve().parent.parent / "docs" / "mockups" / "on" / "_shared" / "on_data.js"


def main() -> int:
    url = f"{BASE}/on/data"
    print(f"Snapshot ON desde {url} …")
    try:
        with urlopen(url, timeout=30) as r:  # noqa: S310 (localhost, read-only)
            payload = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR bajando {url}: {e}  (¿está el server vivo en :8000?)", file=sys.stderr)
        return 1

    bonds = payload.get("bonds") or []
    if not bonds:
        print("Sin bonos — ¿está el server vivo y con datos?", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    js = ("/* AUTO-GENERADO por scripts/build_on_mockup_data.py desde /on/data — NO editar a mano.\n"
          f"   Snapshot real del panel ON: {payload.get('generated', '?')}. */\n"
          "window.ON_DATA = " + json.dumps(payload, ensure_ascii=False, indent=1) + ";\n")
    OUT.write_text(js, encoding="utf-8")

    meta = payload.get("meta", {})
    n_clase = sum(1 for b in bonds if b.get("clase"))
    print(f"\nOK -> {OUT}")
    print(f"  {meta.get('n_bonds', '?')} ONs (MEP)  ·  AR={meta.get('n_ar', '?')}  "
          f"EXT={meta.get('n_ext', '?')}  ·  {len(bonds)} patas  ·  {n_clase} con clase")
    sectors = payload.get("sectors", [])
    if sectors:
        print("  sectores: " + ", ".join(f"{s['label']}({s['count']})" for s in sectors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
