"""Alta de LMS8O — Aluar Aluminio Argentino S.A.I.C. - Serie 8 (ON Hard Dollar).

Cashflows del cuadro BYMA (100 VN):
  - 21/09/2024: primer cupón semestral largo 3.19% (emis 21/03/2024)
  - Trimestrales hasta 21/03/2026 (sin amort)
  - 4 amortizaciones trimestrales 25% c/u: 21/06/2026 → 21/03/2027

ACT/365 (igual que el resto de las ONs hard-dollar). Ley AR. Paga en Cable.
Sector auto-detectado: "ALUAR" → Industrial / Maquinaria.

    py -3.12 scripts/ingest_on_lms8o.py            # alta + verificación
    py -3.12 scripts/ingest_on_lms8o.py --dry-run  # sólo muestra cashflows
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.op_guards import guard_write  # noqa: E402

SHEET = "Obligaciones_Negociables"

CASHFLOWS = [
    {"date": "2024-09-21", "interest": 3.19,  "amortization": 0.0},
    {"date": "2024-12-21", "interest": 1.58,  "amortization": 0.0},
    {"date": "2025-03-21", "interest": 1.56,  "amortization": 0.0},
    {"date": "2025-06-21", "interest": 1.60,  "amortization": 0.0},
    {"date": "2025-09-21", "interest": 1.60,  "amortization": 0.0},
    {"date": "2025-12-21", "interest": 1.58,  "amortization": 0.0},
    {"date": "2026-03-21", "interest": 1.56,  "amortization": 0.0},
    {"date": "2026-06-21", "interest": 1.60,  "amortization": 25.0},
    {"date": "2026-09-21", "interest": 1.20,  "amortization": 25.0},
    {"date": "2026-12-21", "interest": 0.79,  "amortization": 25.0},
    {"date": "2027-03-21", "interest": 0.39,  "amortization": 25.0},
]

FIELDS = {
    "ticker_ars":           "LMS8O",
    "ticker_mep":           "LMS8D",
    "ticker_ccl":           "LMS8C",
    "isin":                 "AR0787528089",
    "short_name":           "Aluar Aluminio Argentino S.A.I.C.",
    "tipo":                 "HARD DOLLAR",
    # Vocabulario canonico (agents.md / select del ABM): `is_ley_argentina` es
    # "ARGENTIN" in ley.upper(), asi que "AR" caia como ley EXTRANJERA y la pata
    # pesos se valuaba contra CCL en vez de MEP.
    "ley_aplicable":        "Argentina",
    "serie_clase":          "Serie 8",
    "fecha_emision":        "2024-03-21",
    "fecha_vencimiento":    "2027-03-21",
    "cupon anual %":        "6.25",
    "frecuencia pagos":     "4",
    "base calculo":         "ACT/365",
    "tipo amortizacion":    "amortizing",
}


def main(dry_run: bool = False, force: bool = False) -> int:
    print("== LMS8O cashflows ==")
    today_str = date.today().isoformat()
    fut = [x for x in CASHFLOWS if x["date"] > today_str]
    for x in CASHFLOWS:
        tag = " <- amort" if x["amortization"] else ""
        mark = " [HOY>]" if x["date"] <= today_str else ""
        print(f"  {x['date']}  int={x['interest']:5.2f}  amort={x['amortization']:6.2f}{tag}{mark}")
    print(f"  residual futuro (sum amort): {sum(x['amortization'] for x in fut):.2f}")
    if dry_run:
        print("\n== DRY RUN (no escribe) ==")
        return 0

    from apps.web.instruments_abm import save_instrument, get_instrument
    from scripts.load_bond import verify

    if (rc := guard_write("pre-lms8o", force=force)):
        return rc

    pre = "ya existía" if get_instrument("LMS8O") else "nuevo"
    res = save_instrument(SHEET, FIELDS, cashflows=CASHFLOWS)
    print(f"{res['action']:8} {', '.join(res['tickers'])}  (Aluar Serie 8) [{pre}]")

    print("\nVERIFICACIÓN (settle T+1 desde hoy)")
    r = verify("LMS8D", price=100.0, price_mode="dirty")
    if not r or r.get("error"):
        print(f"LMS8D  ERROR: {r.get('error') if r else 'sin resultado'}")
        return 1
    tir  = (r.get("tir") or 0) * 100
    md   = r.get("duration")
    vt   = r.get("technical_value")
    accr = r.get("accrued_interest")
    vr   = r.get("residual_nominal")
    par  = (r.get("parity") or 0) * 100
    print(f"  TIR     {tir:.2f}%")
    print(f"  MD      {md:.2f}")
    print(f"  VT      {vt:.2f}")
    print(f"  Accrued {accr:.2f}")
    print(f"  VR      {vr:.2f}")
    print(f"  Paridad {par:.2f}%")
    print("\nReiniciá el server para verla en el panel ON.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv,
                          force="--force" in sys.argv))
