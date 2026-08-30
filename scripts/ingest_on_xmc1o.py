"""Alta de XMC1O — Minera Exar S.A. Clase I u$s 8% (ON hard-dollar AMORTIZING).

    XMC1O / XMC1D / XMC1C   ISIN AR0778941762   Ley ARG (paga MEP)   ACT/365
    emisión 11/11/2024 → vto 11/11/2027   8.000% TNA semestral
    amortiza 50% + 50% en los dos últimos cupones (11/05/2027 y 11/11/2027)

CASHFLOW EXPLÍCITO (no sintetizable): el synth quedó reducido a bullet/ZC (commit
85e5180 — `TestAmortizingIsExplicitNow`), así que un amortizing se carga con el
cashflow publicado. Montos per 100 VN, autoritativos (pestaña CashFlow de Balanz):

    fecha        renta   amort   (obs)            VR
    11/11/2024   0.00    0.00    ancla emisión    100
    11/05/2025   3.97    0.00    renta            100   (8% × 181/365)
    11/11/2025   4.03    0.00    renta            100   (8% × 184/365)
    11/05/2026   3.97    0.00    renta            100
    11/11/2026   4.03    0.00    renta            100
    11/05/2027   3.97   50.00    renta + amort     50   (renta sobre 100, amort 50)
    11/11/2027   2.02   50.00    renta + amort      0   (8% × 184/365 × 50, vto)

DB-only (save_instrument append, NO destructivo). Snapshot pre-op. Idempotente.

    py -3.12 scripts/ingest_on_xmc1o.py            # alta + verificación
    py -3.12 scripts/ingest_on_xmc1o.py --dry-run  # solo arma/mira el cashflow
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SHEET = "Obligaciones_Negociables"

FIELDS = {
    "ticker_ars": "XMC1O", "ticker_mep": "XMC1D", "ticker_ccl": "XMC1C",
    "isin": "AR0778941762",
    "short_name": "Minera Exar S.A. - Clase I",
    "serie_clase": "Clase I", "tipo": "HARD DOLLAR", "ley_aplicable": "Argentina",
    "fecha_emision": "2024-11-11", "fecha_vencimiento": "2027-11-11",
    "cupon anual %": "8", "frecuencia pagos": "2",
    "base calculo": "ACT/365", "tipo amortizacion": "amortizing",
}

# Cashflow publicado (Balanz), per 100 VN. La 1ª fila (11/11/2024, renta 0) es el ancla
# del período corriente — NO un pago. Las dos últimas amortizan 50% c/u.
CASHFLOWS = [
    {"date": "2024-11-11", "interest": 0.00, "amortization": 0.00},
    {"date": "2025-05-11", "interest": 3.97, "amortization": 0.00},
    {"date": "2025-11-11", "interest": 4.03, "amortization": 0.00},
    {"date": "2026-05-11", "interest": 3.97, "amortization": 0.00},
    {"date": "2026-11-11", "interest": 4.03, "amortization": 0.00},
    {"date": "2027-05-11", "interest": 3.97, "amortization": 50.00},
    {"date": "2027-11-11", "interest": 2.02, "amortization": 50.00},
]

# Ground-truth de la calculadora de Balanz (pestaña Calculadora, T+1 → 17/06/2026,
# precio dirty 103.35, sobre la pata …D en USD).
VERIFY_PRICE = 103.35
REF = {"tir": 5.76, "tna_nom": 5.68, "md": 1.08, "vt": 100.81,
       "accrued": 0.81, "clean": 102.539, "vr": 100.0, "parity": 102.52}
SETTLE = date(2026, 6, 17)   # T+1 hábil del 16/06/2026 (= fecha de liquidación de Balanz)


def _fmt(v, nd=2):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "—"


def main(dry_run: bool) -> int:
    fut = [x for x in CASHFLOWS if x["date"] > SETTLE.isoformat()]
    print("== XMC1O  Minera Exar S.A. Clase I  ISIN AR0778941762  [explícito · amortizing] ==")
    print(f"   {len(CASHFLOWS)} flujos; futuros (>settle {SETTLE}):")
    for x in fut:
        tag = "  <- renta+amort" if x["amortization"] else ""
        print(f"     {x['date']}  int={x['interest']:6.2f}  amort={x['amortization']:6.2f}{tag}")
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
                     keep=settings.backup_keep, tag="pre-xmc1o")
    print(f"backup pre-op: {snap}")
    pre = "ya existía" if get_instrument("XMC1O") else "nuevo"
    save = save_instrument(SHEET, FIELDS, cashflows=CASHFLOWS)
    print(f"{save['action']}: {', '.join(save['tickers'])}  [{pre}]  ({save.get('cashflows')} cashflows)\n")

    # Verificación contra Balanz sobre la pata …D (USD, sin FX), dirty 103.35, T+1.
    r = verify("XMC1D", price=VERIFY_PRICE, price_mode="dirty")
    if not r or r.get("error"):
        print(f"ERROR verify: {r.get('error') if r else 'sin resultado'}")
        return 1
    got = {"tir": (r.get("tir") or 0) * 100, "tna_nom": (r.get("tna_nominal") or 0) * 100,
           "md": r.get("duration"), "vt": r.get("technical_value"),
           "accrued": r.get("accrued_interest"), "clean": r.get("price_clean"),
           "vr": r.get("residual_nominal"), "parity": (r.get("parity") or 0) * 100}
    hdr = (f"{'':6} {'TIR%':>7} {'TNAn%':>7} {'MD':>6} {'VT':>8} {'accr':>6} "
           f"{'clean':>9} {'VR':>7} {'parity%':>8}")
    print("VERIFICACION  (engine -> Balanz)\n" + hdr)
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
            diffs.append(f"{k} {_fmt(g, 3)}≠{REF[k]} (Δ{_fmt((g or 0)-REF[k],3)})")
    print("\n" + ("✓ XMC1O reconcilia con Balanz." if not diffs
                  else "⚠ diverge: " + "; ".join(diffs)))
    print("Reiniciá el server (o esperá el reload del repo) para verla en el panel ON.")
    return 0 if not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv))
