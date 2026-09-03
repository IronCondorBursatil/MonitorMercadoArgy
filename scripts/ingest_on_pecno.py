"""Alta de PECNO — Petrolera Aconcagua Energía S.A. Clase XXII u$s (ex Tango Energy).

    PECNO / PECND / PECNC   ISIN AR0199819753   Ley Local (paga MEP)   ACT/365
    emisión 26/08/2025 → vto 25/08/2032   step-up + amortizing (anual)

CASHFLOW EXPLÍCITO (no sintetizable): cupón STEP-UP sobre saldo declinante
(3 / 3 / 3 / 5 / 6 / 7 / 7 %) + amortización 20/20/60 en los 3 últimos años. El synth
de un solo `cupon anual %` no reproduce el step-up → se carga el cashflow publicado
(pestaña CashFlow de la fuente, montos per 100 VN, autoritativos):

    fecha        renta   amort   (obs)
    25/08/2025    0.00    0.00    ancla del 1er período (referencia "Último cupón")
    25/08/2026    3.00    0.00    renta
    25/08/2027    3.00    0.00    renta
    25/08/2028    3.01    0.00    renta (3% × 366/365, 2028 bisiesto)
    25/08/2029    5.00    0.00    renta
    25/08/2030    6.00   20.00    renta + amort
    25/08/2031    5.60   20.00    renta + amort  (7% × 80)
    25/08/2032    4.21   60.00    renta + amort  (7% × 60 × 366/365, vto)

La fila ancla 25/08/2025 (renta 0) fija el inicio del período corriente en esa fecha
(= "Último cupón" de la referencia) → intereses corridos = 3 × 296/365 = 2.43 al settle T+1
(17/06/2026), V.Téc = 100 + 2.43 = 102.43. Reconciliado además contra el PPV publicado
(5.13 = vida promedio ponderada de TODOS los flujos) → el schedule queda triple-validado.

DB-only (save_instrument append, NO destructivo). Snapshot pre-op. Idempotente.

    py -3.12 scripts/ingest_on_pecno.py            # alta + verificación
    py -3.12 scripts/ingest_on_pecno.py --dry-run  # solo arma/mira el cashflow
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.op_guards import guard_write  # noqa: E402

SHEET = "Obligaciones_Negociables"

FIELDS = {
    "ticker_ars": "PECNO", "ticker_mep": "PECND", "ticker_ccl": "PECNC",
    "isin": "AR0199819753",
    "short_name": "Petrolera Aconcagua Energía S.A. - Clase XXII",
    "serie_clase": "Clase XXII", "tipo": "HARD DOLLAR", "ley_aplicable": "Argentina",
    "fecha_emision": "2025-08-26", "fecha_vencimiento": "2032-08-25",
    "cupon anual %": "3", "frecuencia pagos": "1",   # display (step-up real va en el cashflow)
    "base calculo": "ACT/365", "tipo amortizacion": "amortizing",
    "denom_base": "1.00", "denom_incremento": "1.00", "valor_nominal": "1.00",
}

# Cashflow publicado por la fuente, per 100 VN. La 1ª fila (25/08/2025, renta 0) es el ancla
# del período corriente — NO un pago.
CASHFLOWS = [
    {"date": "2025-08-25", "interest": 0.00, "amortization": 0.00},
    {"date": "2026-08-25", "interest": 3.00, "amortization": 0.00},
    {"date": "2027-08-25", "interest": 3.00, "amortization": 0.00},
    {"date": "2028-08-25", "interest": 3.01, "amortization": 0.00},
    {"date": "2029-08-25", "interest": 5.00, "amortization": 0.00},
    {"date": "2030-08-25", "interest": 6.00, "amortization": 20.00},
    {"date": "2031-08-25", "interest": 5.60, "amortization": 20.00},
    {"date": "2032-08-25", "interest": 4.21, "amortization": 60.00},
]

# Ground-truth de la calculadora de referencia (pestaña Calculadora, T+1 → 17/06/2026; sin
# precio cargado → solo las medidas precio-independientes + PPV/term).
REF = {"vt": 102.43, "accrued": 2.43, "vr": 100.0, "ppv": 5.13, "ttm": 6.19, "dias": 296}
SETTLE = date(2026, 6, 17)   # T+1 hábil del 16/06/2026 (= fecha de liquidación de la referencia)


def _fmt(v, nd=2):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "—"


def _ppv(cfs, settle):
    """Vida promedio ponderada (años) = Σ(total × t) / Σ total, t = días/365 desde settle.
    Convención de la calculadora (sin descontar)."""
    num = den = 0.0
    for x in cfs:
        d = date.fromisoformat(x["date"])
        if d <= settle:
            continue
        tot = x["interest"] + x["amortization"]
        t = (d - settle).days / 365.0
        num += tot * t
        den += tot
    return num / den if den else None


def main(dry_run: bool = False, force: bool = False) -> int:
    fut = [x for x in CASHFLOWS if x["date"] > SETTLE.isoformat()]
    print("== PECNO  Petrolera Aconcagua Energía Clase XXII  ISIN AR0199819753  [explícito] ==")
    print(f"   {len(CASHFLOWS)} flujos; futuros (>settle {SETTLE}):")
    for x in fut:
        tag = "  <- renta+amort" if x["amortization"] else ""
        print(f"     {x['date']}  int={x['interest']:6.2f}  amort={x['amortization']:6.2f}{tag}")
    res_amort = sum(x["amortization"] for x in fut)
    ppv = _ppv(CASHFLOWS, SETTLE)
    print(f"   Σ amort futura (residual) = {res_amort:.2f}  ·  PPV calc = {_fmt(ppv)}  (referencia {REF['ppv']})")
    print()
    if dry_run:
        print("== DRY RUN (no escribe) ==")
        return 0

    from apps.web.instruments_abm import save_instrument, get_instrument
    from scripts.load_bond import verify

    if (rc := guard_write("pre-pecno", force=force)):
        return rc
    pre = "ya existía" if get_instrument("PECNO") else "nuevo"
    save = save_instrument(SHEET, FIELDS, cashflows=CASHFLOWS)
    print(f"{save['action']}: {', '.join(save['tickers'])}  [{pre}]  ({save.get('cashflows')} cashflows)\n")

    # Verificación: VT / accrued / VR son PRECIO-INDEPENDIENTES → comparan directo contra
    # la referencia (le pasamos dirty=VT solo para fijar un precio; no es un precio de mercado).
    r = verify("PECND", price=REF["vt"], price_mode="dirty")
    if not r or r.get("error"):
        print(f"ERROR verify: {r.get('error') if r else 'sin resultado'}")
        return 1
    got = {"vt": r.get("technical_value"), "accrued": r.get("accrued_interest"),
           "vr": r.get("residual_nominal"), "dias": r.get("days_accrued") or r.get("accrued_days"),
           "ppv": ppv}
    print(f"{'':8} {'V.Téc':>8} {'accrued':>8} {'VR':>7} {'PPV':>6}")
    print(f"{'engine':8} {_fmt(got['vt']):>8} {_fmt(got['accrued']):>8} {_fmt(got['vr']):>7} {_fmt(got['ppv']):>6}")
    print(f"{'Ref':8} {REF['vt']:>8} {REF['accrued']:>8} {REF['vr']:>7} {REF['ppv']:>6}")
    diffs = []
    for k, tol in (("vt", 0.05), ("accrued", 0.05), ("vr", 0.05), ("ppv", 0.1)):
        g = got[k]
        if g is None or abs(g - REF[k]) > tol:
            diffs.append(f"{k} {_fmt(g)}≠{REF[k]} (tol {tol})")
    # info extra (precio-dependiente, sin ground-truth de precio en la imagen)
    tir = (r.get("tir") or 0) * 100
    print(f"\n   (info @dirty {REF['vt']}: TIR {_fmt(tir)}%  MD {_fmt(r.get('duration'))}  "
          f"clean {_fmt(r.get('price_clean'))})")
    print("\n" + ("✓ PECNO reconcilia con la referencia (V.Téc/accrued/VR/PPV)." if not diffs
                  else "⚠ diverge: " + "; ".join(diffs)))
    print("Reiniciá el server (o esperá el reload del repo) para verla en el panel ON.")
    return 0 if not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv,
                          force="--force" in sys.argv))
