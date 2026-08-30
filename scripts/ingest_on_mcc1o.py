"""Alta de MCC1O - PECOM Servicios Energia S.A.U. Clase I u$s 7.9% (ON hard-dollar BULLET).

    MCC1O / MCC1D / MCC1C   ISIN AR0518594251   Ley ARG (paga MEP)   ACT/365
    emision 10/03/2025 -> vto 10/03/2029   7.900% TNA semestral   bullet

Bullet regular, emision en dia de cupon -> synth limpio (8 cupones + bullet al vto).
Los cupones varian por dias reales (ACT/365): 3.98/3.92/3.98/3.92/3.98/3.94/3.98/3.92.

DB-only (save_instrument append, NO destructivo). Snapshot pre-op. Idempotente.

    py -3.12 scripts/ingest_on_mcc1o.py            # alta + verificacion
    py -3.12 scripts/ingest_on_mcc1o.py --dry-run  # solo mira la grilla synth
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SHEET = "Obligaciones_Negociables"

FIELDS = {
    "ticker_ars": "MCC1O", "ticker_mep": "MCC1D", "ticker_ccl": "MCC1C",
    "isin": "AR0518594251",
    "short_name": "PECOM Servicios Energia S.A.U. - Clase I",
    "serie_clase": "Clase I", "tipo": "HARD DOLLAR", "ley_aplicable": "Argentina",
    "fecha_emision": "2025-03-10", "fecha_vencimiento": "2029-03-10",
    "cupon anual %": "7.9", "frecuencia pagos": "2",
    "base calculo": "ACT/365", "tipo amortizacion": "bullet",
}

# Balanz (Calculadora, dirty 104.50, T+1 -> 17/06/2026)
VERIFY_PRICE = 104.50
REF = {"tir": 7.05, "tna_nom": 6.93, "md": 2.38, "vt": 102.14,
       "accrued": 2.14, "clean": 102.357, "vr": 100.0, "parity": 102.31}


def _fmt(v, nd=2):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "--"


def main(dry_run: bool) -> int:
    from apps.web.instruments_abm import _safe_synth

    synth = _safe_synth(FIELDS)
    fut = [cf for cf in synth if cf.date.isoformat() > "2026-06-17"]
    print(f"grilla synth: {len(synth)} cupones; futuros (>settle 17/06/2026):")
    for cf in synth:
        tag = "  <- cupon+amort (bullet)" if cf.amortization else ""
        mark = " *" if cf.date.isoformat() > "2026-06-17" else ""
        print(f"  {cf.date}  int={cf.interest:7.4f}  amort={cf.amortization:7.2f}{tag}{mark}")
    print(f"Suma amort futura (residual) = {sum(cf.amortization for cf in fut):.2f}")
    print()
    if dry_run:
        print("== DRY RUN (no escribe) ==")
        return 0

    from config.settings import settings
    from core.infrastructure.db.backup import backup_db
    from apps.web.instruments_abm import save_instrument
    from scripts.load_bond import verify

    snap = backup_db(settings.catalog_db, settings.backup_dir,
                     keep=settings.backup_keep, tag="pre-mcc1o")
    print(f"backup pre-op: {snap}")
    res = save_instrument(SHEET, FIELDS, cashflows=None)
    print(f"{res['action']}: {', '.join(res['tickers'])}\n")

    r = verify("MCC1D", price=VERIFY_PRICE, price_mode="dirty")
    if not r or r.get("error"):
        print(f"ERROR verify: {r.get('error') if r else 'sin resultado'}")
        return 1
    got = {"tir": (r.get("tir") or 0) * 100, "tna_nom": (r.get("tna_nominal") or 0) * 100,
           "md": r.get("duration"), "vt": r.get("technical_value"),
           "accrued": r.get("accrued_interest"), "clean": r.get("price_clean"),
           "vr": r.get("residual_nominal"), "parity": (r.get("parity") or 0) * 100}
    print(f"{'':6} {'TIR%':>7} {'TNAn%':>7} {'MD':>6} {'VT':>8} {'accr':>6} {'clean':>9} {'VR':>7} {'parity%':>8}")
    print(f"{'engine':6} {_fmt(got['tir']):>7} {_fmt(got['tna_nom']):>7} {_fmt(got['md']):>6} "
          f"{_fmt(got['vt']):>8} {_fmt(got['accrued']):>6} {_fmt(got['clean'],3):>9} "
          f"{_fmt(got['vr']):>7} {_fmt(got['parity']):>8}")
    print(f"{'Balanz':6} {REF['tir']:>7} {REF['tna_nom']:>7} {REF['md']:>6} "
          f"{REF['vt']:>8} {REF['accrued']:>6} {REF['clean']:>9.3f} {REF['vr']:>7} {REF['parity']:>8}")
    diffs = []
    for k, tol in (("tir", 0.4), ("md", 0.06), ("vt", 0.06), ("accrued", 0.06),
                   ("clean", 0.06), ("vr", 0.06), ("parity", 0.4)):
        g = got[k]
        if g is None or abs(g - REF[k]) > tol:
            diffs.append(f"{k} {_fmt(g, 3)} != {REF[k]} (d={_fmt((g or 0) - REF[k], 3)})")
    print()
    print("OK MCC1O reconcilia con Balanz." if not diffs
          else "DIVERGE: " + "; ".join(diffs))
    print("Reinicia el server para verla en el panel ON.")
    return 0 if not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv))
