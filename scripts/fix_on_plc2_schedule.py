"""Corrige el calendario de cupones de PLC2 (Pluspetrol S.A. - Clase 2, AR0036431960).

    PLC2O / PLC2D / PLC2C   ISIN AR0036431960   Ley ARG   7.5% TNA semestral, ACT/365
    emisión 27/01/2025 → vto 27/01/2030

Bug: la ON se había sintetizado como bullet anclando la grilla a `fecha_emision`
(27/01) → cupones en 27/07 y 27/01. PERO el bono realmente paga **27/04 y 27/10**,
con un stub final corto (27/10/2029 → 27/01/2030 = vto). El calendario synth ponía
el último cupón en 27/01/2026 (150 días de accrued = 3.08) en vez de 27/04/2026
(60 días = 1.23) → clean 102.9 en vez de 104.77 → TIR 6.68% en vez de 6.09%.

Caso irregular (stubs primero/último) → cashflows EXPLÍCITOS (política del repo:
synth sólo bullet/ZC). Cupón ACT/365 = 7.5 × días_período / 365. Reconcilia EXACTO
con la calculadora de Balanz (leg …D, dirty 106, T+1 → 26/06/2026):
    TIR 6.09 · TNA nom 6.00 · MD 3.09 · V.Téc 101.23 · accrued 1.23 · clean 104.77 · CY 7.16

Corrección quirúrgica vía `save_cashflows` (sólo reemplaza los flujos; la fila —
tickers, ley, ISIN, sector, etc.— queda intacta). Snapshot pre-op. Idempotente.

    py -3.12 scripts/fix_on_plc2_schedule.py            # corrige + verifica
    py -3.12 scripts/fix_on_plc2_schedule.py --dry-run  # sólo arma/mira la grilla
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:  # consola Windows cp1252 → forzar UTF-8 para los glifos (Σ, ✓, ·)
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

PRIMARY = "PLC2O"
RATE = 7.5  # % TNA

# Fechas de pago de cupón: 27/04 y 27/10. Primer período = stub corto desde la
# emisión (27/01/2025 → 27/04/2025); último = stub corto al vto (27/10/2029 → 27/01/2030).
_ANCHORS: List[date] = [
    date(2025, 1, 27),  # emisión (inicio de devengamiento, no paga)
    date(2025, 4, 27), date(2025, 10, 27),
    date(2026, 4, 27), date(2026, 10, 27),
    date(2027, 4, 27), date(2027, 10, 27),
    date(2028, 4, 27), date(2028, 10, 27),
    date(2029, 4, 27), date(2029, 10, 27),
    date(2030, 1, 27),  # vto (stub final + amortización)
]
MATURITY = date(2030, 1, 27)


def build_cashflows() -> List[Dict[str, object]]:
    """Cupón ACT/365 sobre cada período real + amortización bullet (100) al vto."""
    out: List[Dict[str, object]] = []
    for i in range(1, len(_ANCHORS)):
        d0, d1 = _ANCHORS[i - 1], _ANCHORS[i]
        interest = round(RATE * (d1 - d0).days / 365.0, 6)
        amort = 100.0 if d1 == MATURITY else 0.0
        out.append({"date": d1.isoformat(), "interest": interest, "amortization": amort})
    return out


# Balanz (leg …D, dirty 106, T+1 → 26/06/2026)
VERIFY_PRICE = 106.0
REF = {"tir": 6.09, "tna_nom": 6.00, "md": 3.09, "vt": 101.23,
       "accrued": 1.23, "clean": 104.77, "vr": 100.0, "parity": 104.71}


def _fmt(v, nd=2):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "—"


def main(dry_run: bool) -> int:
    cfs = build_cashflows()
    fut = [c for c in cfs if str(c["date"]) > "2026-06-26"]
    print(f"calendario explícito (27/04 · 27/10 + stubs): {len(cfs)} cupones; futuros (>settle):")
    for c in cfs:
        past = "" if str(c["date"]) > "2026-06-26" else "  (pasado)"
        tag = "  <- stub final + amort" if c["amortization"] else ""
        print(f"  {c['date']}  int={c['interest']:8.4f}  amort={c['amortization']:6.1f}{tag}{past}")
    print(f"residual (Σ amort futura) = {sum(c['amortization'] for c in fut):.2f}\n")
    if dry_run:
        print("== DRY RUN (no escribe) ==")
        return 0

    from config.settings import settings
    from core.infrastructure.db.backup import backup_db
    from apps.web.instruments_abm import save_cashflows
    from scripts.load_bond import verify

    snap = backup_db(settings.catalog_db, settings.backup_dir,
                     keep=settings.backup_keep, tag="pre-plc2-schedule")
    print(f"backup pre-op: {snap}")
    res = save_cashflows(PRIMARY, cfs)
    print(f"save_cashflows {res['ticker']}: {res['count']} flujos\n")

    r = verify("PLC2D", price=VERIFY_PRICE, price_mode="dirty")
    if not r or r.get("error"):
        print(f"ERROR verify: {r.get('error') if r else 'sin resultado'}")
        return 1
    got = {"tir": (r.get("tir") or 0) * 100, "tna_nom": (r.get("tna_nominal") or 0) * 100,
           "md": r.get("duration"), "vt": r.get("technical_value"),
           "accrued": r.get("accrued_interest"), "clean": r.get("price_clean"),
           "vr": r.get("residual_nominal"), "parity": (r.get("parity") or 0) * 100}
    hdr = f"{'':6} {'TIR%':>7} {'TNAn%':>7} {'MD':>6} {'VT':>8} {'accr':>6} {'clean':>9} {'VR':>7} {'parity%':>8}"
    print(hdr)
    print(f"{'engine':6} {_fmt(got['tir']):>7} {_fmt(got['tna_nom']):>7} {_fmt(got['md']):>6} "
          f"{_fmt(got['vt']):>8} {_fmt(got['accrued']):>6} {_fmt(got['clean'],4):>9} "
          f"{_fmt(got['vr']):>7} {_fmt(got['parity']):>8}")
    print(f"{'Balanz':6} {REF['tir']:>7} {REF['tna_nom']:>7} {REF['md']:>6} "
          f"{REF['vt']:>8} {REF['accrued']:>6} {REF['clean']:>9.4f} {REF['vr']:>7} {REF['parity']:>8}")
    diffs = []
    for k, tol in (("tir", 0.1), ("tna_nom", 0.1), ("md", 0.03), ("vt", 0.06),
                   ("accrued", 0.03), ("clean", 0.06), ("vr", 0.06), ("parity", 0.2)):
        g = got[k]
        if g is None or abs(g - REF[k]) > tol:
            diffs.append(f"{k} {_fmt(g,4)}≠{REF[k]} (tol {tol})")
    print("\n" + ("✓ PLC2 reconcilia con Balanz (TIR 6.09%)." if not diffs
                  else "⚠ diverge: " + "; ".join(diffs)))
    print("Reiniciá el server (o esperá el reload del repo) para verla corregida en el panel ON.")
    return 0 if not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv))
