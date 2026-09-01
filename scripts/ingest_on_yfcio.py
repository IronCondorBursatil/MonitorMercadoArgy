"""Alta de YFCIO - YPF Energia Electrica S.A. Clase XVII u$s 5.9% (ON hard-dollar).

    YFCIO / YFCID / YFCIC   ISIN AR0947502206   Ley ARG (paga MEP)   30/360
    emision 13/06/2024 -> vto 13/06/2027   5.900% TNA trimestral (freq=4)

CASHFLOW EXPLICITO — no sintetizable por tres razones combinadas:
  1. Primer cupon LARGO: 13/06/2024 -> 13/03/2025 (9 meses = 3 trimestres) = 4.43
  2. Amortizacion en fecha FUERA de grilla: 13/05/2027 (50%, renta 0) — antes del vto
  3. Vto 13/06/2027: 50% amort + 0.74 renta (1 trimestre sobre VR 50)

Montos per 100 VN, autoritativos (pestana CashFlow de la fuente). Base 30/360 trimestral:
  cupon regular = 100 x 5.9% / 4 = 1.475 ~ 1.48
  cupon largo   = 100 x 5.9% x 9/12 = 4.425 ~ 4.43
  cupon vto     = 50  x 5.9% / 4 = 0.7375 ~ 0.74

DB-only (save_instrument append, NO destructivo). Snapshot pre-op. Idempotente.

    py -3.12 scripts/ingest_on_yfcio.py            # alta + verificacion
    py -3.12 scripts/ingest_on_yfcio.py --dry-run  # solo muestra el cashflow
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SHEET = "Obligaciones_Negociables"

FIELDS = {
    "ticker_ars": "YFCIO", "ticker_mep": "YFCID", "ticker_ccl": "YFCIC",
    "isin": "AR0947502206",
    "short_name": "YPF Energia Electrica S.A. - Clase XVII",
    "serie_clase": "Clase XVII", "tipo": "HARD DOLLAR", "ley_aplicable": "Argentina",
    "fecha_emision": "2024-06-13", "fecha_vencimiento": "2027-06-13",
    "cupon anual %": "5.9", "frecuencia pagos": "4",
    "base calculo": "30/360", "tipo amortizacion": "amortizing",
}

# Cashflow publicado por la fuente, per 100 VN.
# Ancla: 13/06/2024 (emision). 1er cupon largo cubre 9 meses (3 trimestres).
# Amort 50% en 13/05/2027 (no es fecha de cupon, renta=0) + 50% al vto 13/06/2027.
CASHFLOWS = [
    {"date": "2024-06-13", "interest": 0.00, "amortization": 0.00},   # ancla emision
    {"date": "2025-03-13", "interest": 4.43, "amortization": 0.00},   # cupon largo 9m (3 trimestres)
    {"date": "2025-06-13", "interest": 1.48, "amortization": 0.00},   # trimestral regular
    {"date": "2025-09-13", "interest": 1.48, "amortization": 0.00},
    {"date": "2025-12-13", "interest": 1.48, "amortization": 0.00},
    {"date": "2026-03-13", "interest": 1.48, "amortization": 0.00},
    {"date": "2026-06-13", "interest": 1.48, "amortization": 0.00},
    {"date": "2026-09-13", "interest": 1.48, "amortization": 0.00},
    {"date": "2026-12-13", "interest": 1.48, "amortization": 0.00},
    {"date": "2027-03-13", "interest": 1.48, "amortization": 0.00},
    {"date": "2027-05-13", "interest": 0.00, "amortization": 50.00},  # amort fuera de grilla, sin renta
    {"date": "2027-06-13", "interest": 0.74, "amortization": 50.00},  # vto: 50%amort + 1 trim sobre VR50
]

# Ground-truth la referencia (Calculadora, dirty 102, T+1 -> 17/06/2026, pata D).
VERIFY_PRICE = 102.0
SETTLE = date(2026, 6, 17)
REF = {"tir": 3.35, "tna_nom": 3.31, "md": 0.92, "vt": 100.07,
       "accrued": 0.07, "clean": 101.934, "vr": 100.0, "parity": 101.93}


def _fmt(v, nd=2):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "--"


def main(dry_run: bool) -> int:
    fut = [x for x in CASHFLOWS if x["date"] > SETTLE.isoformat()]
    print("== YFCIO  YPF Energia Electrica S.A. Clase XVII  ISIN AR0947502206  [explicito] ==")
    print(f"   {len(CASHFLOWS)} flujos; futuros marcados con *:")
    for x in CASHFLOWS:
        mark = " *" if x["date"] > SETTLE.isoformat() else "  "
        tag = "  <- amort" if x["amortization"] and not x["interest"] else (
              "  <- renta+amort" if x["amortization"] else "")
        print(f"  {mark} {x['date']}  int={x['interest']:5.2f}  amort={x['amortization']:6.2f}{tag}")
    res_amort = sum(x["amortization"] for x in fut)
    print(f"   Suma amort futura (residual) = {res_amort:.2f}")
    print()
    if dry_run:
        print("== DRY RUN (no escribe) ==")
        return 0

    from config.settings import settings
    from core.infrastructure.db.backup import backup_db
    from apps.web.instruments_abm import save_instrument, get_instrument
    from scripts.load_bond import verify

    snap = backup_db(settings.catalog_db, settings.backup_dir,
                     keep=settings.backup_keep, tag="pre-yfcio")
    print(f"backup pre-op: {snap}")
    pre = "ya existia" if get_instrument("YFCIO") else "nuevo"
    save = save_instrument(SHEET, FIELDS, cashflows=CASHFLOWS)
    print(f"{save['action']}: {', '.join(save['tickers'])}  [{pre}]  ({save.get('cashflows')} cashflows)\n")

    r = verify("YFCID", price=VERIFY_PRICE, price_mode="dirty")
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
    print(f"{'Ref':6} {REF['tir']:>7} {REF['tna_nom']:>7} {REF['md']:>6} "
          f"{REF['vt']:>8} {REF['accrued']:>6} {REF['clean']:>9.3f} {REF['vr']:>7} {REF['parity']:>8}")
    diffs = []
    for k, tol in (("tir", 0.4), ("md", 0.06), ("vt", 0.06), ("accrued", 0.06),
                   ("clean", 0.06), ("vr", 0.06), ("parity", 0.4)):
        g = got[k]
        if g is None or abs(g - REF[k]) > tol:
            diffs.append(f"{k} {_fmt(g, 3)} != {REF[k]} (d={_fmt((g or 0) - REF[k], 3)})")
    print()
    print("OK YFCIO reconcilia con la referencia." if not diffs
          else "DIVERGE: " + "; ".join(diffs))
    print("Reinicia el server para verla en el panel ON.")
    return 0 if not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv))
