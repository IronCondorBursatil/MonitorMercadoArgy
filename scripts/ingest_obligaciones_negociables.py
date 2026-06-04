"""CLI de ingesta de Obligaciones Negociables (lógica en core.infrastructure.on_catalog).

    py -3.12 scripts/ingest_obligaciones_negociables.py            # ingesta a SQLite
    py -3.12 scripts/ingest_obligaciones_negociables.py --validate # VT/YTM vs informe
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.domain.models import MarketSnapshot  # noqa: E402
from core.infrastructure.on_catalog import (  # noqa: E402
    SHEET, ITYPE, ingest, load_rows, row_to_instrument, to_d_ticker,
)

SETTLE = date(2026, 5, 28)  # liquidación del informe (T+1 de 27-May-26)


def validate() -> None:
    """VT calculado (independiente del precio) vs VT del informe + chequeo de
    convención de YTM (precio que reproduce el VT al YTM informado y vuelta)."""
    from core.domain.services import FinancialEngine

    rows = load_rows()
    bad = 0
    print(f"{'ticker':7} {'VTcalc':>8} {'VTinf':>7} {'dif':>7}   {'YTM@par':>8} {'YTMinf':>7}")
    for r in rows:
        inst = row_to_instrument(r)
        snap = MarketSnapshot(instrument=inst, price=None)
        vt = FinancialEngine.calculate_technical_value(snap, indices_provider=None, ref_date=SETTLE)
        vt_ref = float(r["vt_ref"]) if (r.get("vt_ref") or "").strip() else None
        d = (vt - vt_ref) if vt_ref is not None else None
        flag = "  <-- REVISAR" if (d is not None and abs(d) > 0.15) else ""
        bad += bool(flag)
        ytm_ref = float(r["ytm_ref"]) / 100.0 if (r.get("ytm_ref") or "").strip() else None
        ys = "    n/a"
        if ytm_ref is not None:
            px = FinancialEngine.price_from_tir(snap, ytm_ref, settle_date=SETTLE)
            if px:
                yb = FinancialEngine.calculate_tir(snap.model_copy(update={"price": px}), settle_date=SETTLE)
                ys = f"{yb*100:7.2f}" if yb else "    n/a"
        vts = f"{vt_ref:7.2f}" if vt_ref is not None else "    n/a"
        ds = f"{d:7.2f}" if d is not None else "    n/a"
        yr = f"{float(r['ytm_ref']):7.2f}" if (r.get("ytm_ref") or "").strip() else "    n/a"
        print(f"{to_d_ticker(r['ticker_o']):7} {vt:8.2f} {vts} {ds}{flag}   {ys} {yr}")
    print(f"\n{len(rows)-bad}/{len(rows)} VT dentro de +/-0.15 del informe; {bad} a revisar.")


if __name__ == "__main__":
    if "--validate" in sys.argv:
        validate()
    else:
        n = ingest()
        print(f"ingest: {n} ONs cargadas (sheet={SHEET}, type={ITYPE}).")
