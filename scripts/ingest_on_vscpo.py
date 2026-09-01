"""Alta de VSCPO - Vista Energy Argentina S.A.U. Clase XXIV u$s 8% (ON hard-dollar AMORTIZING).

    VSCPO / VSCPD / VSCPC   ISIN AR0637277028   Ley Local / ARG (paga Cable)   ACT/365
    emision 03/05/2024 -> vto 03/05/2029   8.000% TNA semestral
    amortiza 25%x4 en los 4 ultimos cupones (nov2027/may2028/nov2028/may2029)

CASHFLOW EXPLICITO: el synth solo hace bullet/ZC (commit 85e5180); amortizing -> explicito.
Montos per 100 VN, autoritativos (pestaña CashFlow de la fuente).
Interes sobre saldo declinante (ACT/365):
  - periodos plenos (VR 100): 4.03 (184d) / 3.97 (181d)
  - 1er amort nov2027: int 4.03 sobre VR 100 -> VR 75
  - may2028: int 2.99 sobre VR 75 -> VR 50
  - nov2028: int 2.02 sobre VR 50 -> VR 25
  - may2029: int 0.99 sobre VR 25 -> VR 0 (vto)

Nota ley: La referencia muestra Ley ARG / Paga en Cable. Se registra con ley_aplicable="Argentina"
per instruccion del usuario (la pata O usa MEP para pricing); ajustar si se prefiere CCL.

DB-only (save_instrument append, NO destructivo). Snapshot pre-op. Idempotente.

    py -3.12 scripts/ingest_on_vscpo.py            # alta + verificacion
    py -3.12 scripts/ingest_on_vscpo.py --dry-run  # solo muestra el cashflow
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SHEET = "Obligaciones_Negociables"

FIELDS = {
    "ticker_ars": "VSCPO", "ticker_mep": "VSCPD", "ticker_ccl": "VSCPC",
    "isin": "AR0637277028",
    "short_name": "Vista Energy Argentina S.A.U. - Clase XXIV",
    "serie_clase": "Clase XXIV", "tipo": "HARD DOLLAR", "ley_aplicable": "Argentina",
    "fecha_emision": "2024-05-03", "fecha_vencimiento": "2029-05-03",
    "cupon anual %": "8", "frecuencia pagos": "2",
    "base calculo": "ACT/365", "tipo amortizacion": "amortizing",
}

# Cashflow publicado por la fuente, per 100 VN. Ancla 03/05/2024 = fecha de emision.
# Interes sobre saldo declinante; amort 25% en los 4 ultimos cupones.
CASHFLOWS = [
    {"date": "2024-05-03", "interest": 0.00, "amortization": 0.00},  # ancla
    {"date": "2024-11-03", "interest": 4.03, "amortization": 0.00},  # 184d x 8%/365 x 100
    {"date": "2025-05-03", "interest": 3.97, "amortization": 0.00},  # 181d
    {"date": "2025-11-03", "interest": 4.03, "amortization": 0.00},  # 184d
    {"date": "2026-05-03", "interest": 3.97, "amortization": 0.00},  # 181d
    {"date": "2026-11-03", "interest": 4.03, "amortization": 0.00},  # 184d
    {"date": "2027-05-03", "interest": 3.97, "amortization": 0.00},  # 181d
    {"date": "2027-11-03", "interest": 4.03, "amortization": 25.00}, # 184d x VR100; -> VR 75
    {"date": "2028-05-03", "interest": 2.99, "amortization": 25.00}, # 181d x VR75;  -> VR 50
    {"date": "2028-11-03", "interest": 2.02, "amortization": 25.00}, # 184d x VR50;  -> VR 25
    {"date": "2029-05-03", "interest": 0.99, "amortization": 25.00}, # 181d x VR25;  -> VR 0 (vto)
]

# Ground-truth la referencia (Calculadora, dirty 106.20, T+1 -> 17/06/2026, sobre pata D).
VERIFY_PRICE = 106.20
SETTLE = date(2026, 6, 17)
REF = {"tir": 5.44, "tna_nom": 5.36, "md": 1.93, "vt": 100.99,
       "accrued": 0.99, "clean": 105.214, "vr": 100.0, "parity": 105.16}


def _fmt(v, nd=2):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "--"


def main(dry_run: bool) -> int:
    fut = [x for x in CASHFLOWS if x["date"] > SETTLE.isoformat()]
    print("== VSCPO  Vista Energy Argentina S.A.U. Clase XXIV  ISIN AR0637277028  [explicito amortizing] ==")
    print(f"   {len(CASHFLOWS)} flujos; futuros (>settle {SETTLE}):")
    for x in CASHFLOWS:
        mark = " *" if x["date"] > SETTLE.isoformat() else "  "
        tag = "  <- renta+amort" if x["amortization"] else ""
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
                     keep=settings.backup_keep, tag="pre-vscpo")
    print(f"backup pre-op: {snap}")
    pre = "ya existia" if get_instrument("VSCPO") else "nuevo"
    save = save_instrument(SHEET, FIELDS, cashflows=CASHFLOWS)
    print(f"{save['action']}: {', '.join(save['tickers'])}  [{pre}]  ({save.get('cashflows')} cashflows)\n")

    r = verify("VSCPD", price=VERIFY_PRICE, price_mode="dirty")
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
    print("OK VSCPO reconcilia con la referencia." if not diffs
          else "DIVERGE: " + "; ".join(diffs))
    print("Reinicia el server para verla en el panel ON.")
    return 0 if not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv))
