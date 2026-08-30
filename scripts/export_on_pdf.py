"""Exporta el panel de Obligaciones Negociables (/on) a un PDF landscape para cliente.

Toma los datos EN VIVO del server (GET /on/data) y delega el armado del PDF a
`apps.web.on_pdf.build_on_pdf` (el MISMO que usa el botón 🖨️ del panel). El PDF queda
en la raíz del proyecto.

    py -3.12 scripts/export_on_pdf.py            # solo Hard Dollar (como el panel)
    py -3.12 scripts/export_on_pdf.py --all      # + Dollar Linked
    py -3.12 scripts/export_on_pdf.py --no-charts # resumen sin scatter (más rápido)
    py -3.12 scripts/export_on_pdf.py --url http://localhost:8000
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def fetch(url: str) -> dict:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/on/data", timeout=10) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        print(f"  (server no respondió: {e}) — usando TestClient en proceso…")
        import os
        os.environ["MONITOR_DISABLE_LOOPS"] = "1"
        from starlette.testclient import TestClient
        from apps.web.app import app
        with TestClient(app) as c:
            return c.get("/on/data").json()


def _opt(argv, flag: str, default: str) -> str:
    """Valor de `--flag valor`. Sin valor detrás → error de uso, no un IndexError."""
    if flag not in argv:
        return default
    i = argv.index(flag) + 1
    if i >= len(argv) or argv[i].startswith("--"):
        raise SystemExit(f"uso: {flag} <valor>  (falta el valor de {flag})")
    return argv[i]


def main(argv) -> int:
    try:
        from apps.web.on_pdf import build_on_pdf
    except ImportError as e:
        raise SystemExit(f"Falta una dependencia del export ({e}). "
                         "Instalá: py -3.12 -m pip install -r requirements.lock") from e

    include_dl = "--all" in argv
    charts = "--no-charts" not in argv
    url = _opt(argv, "--url", "http://localhost:8000")
    print(f"Bajando datos de {url}/on/data …")
    data = fetch(url)
    blob, meta = build_on_pdf(data, include_dl=include_dl, charts=charts)

    out = ROOT / f"Obligaciones_Negociables_{data.get('today') or 'AR'}.pdf"
    try:
        out.write_bytes(blob)
    except PermissionError:
        out = out.with_name(out.stem + "_nuevo.pdf")
        out.write_bytes(blob)
        print(f"  ⚠ El PDF original está abierto/bloqueado — guardé en «{out.name}». "
              "Cerrá el visor para sobrescribir el nombre original.")

    kind = "HD + Dollar Linked" if include_dl else "Hard Dollar"
    print(f"✓ {out.name}  ({meta['bonds']} ONs {kind} · {meta['sectors']} sectores · "
          f"{meta['pages']} págs{' · sin charts' if not charts else ''})")
    print(f"  {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
