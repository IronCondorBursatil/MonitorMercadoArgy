"""Alta de 4 ONs hard-dollar (verificadas contra la calculadora de referencia, T+1 → 16/06/2026):

    RC1CO  ARCOR Clase 1        7.600%  USP04559BE29  amort 33/33/34 (2031-33) → explícito
    RAC7O  RAGHSA Clase 7       8.500%  USP79849AF54  bullet                   → synth
    RAC5O  RAGHSA Clase 5       8.250%  USP79849AE89  bullet                   → synth
    C138O  CGC Clase 38 (Adic.) 11.875% USP3063DAD41  bullet                   → synth

Nota ISIN RAC5O: la calculadora de referencia mostraba US750645AG86, pero el ISIN
autoritativo es el de BYMADATA (byma_catalog) = USP79849AE89.

RC1CO es amortizing (33%+33%+34% en los 3 julios finales 2031/2032/2033) → cashflow
EXPLÍCITO (cupón flat sobre saldo declinante). Los 3 RAGHSA/CGC son bullet con día de
emisión = día de vto (la grilla synth ancla limpio) → ABM synth. Todos 30/360, Ley NY.

DB-only (save_instrument append, NO destructivo). Snapshot pre-op. Idempotente.

    py -3.12 scripts/ingest_ons_arcor_raghsa_cgc.py            # alta + verificación
    py -3.12 scripts/ingest_ons_arcor_raghsa_cgc.py --dry-run  # sólo arma/mira cashflows
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from dateutil.relativedelta import relativedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.op_guards import guard_write  # noqa: E402

SHEET = "Obligaciones_Negociables"


def _grid(start: date, end: date, months: int) -> List[date]:
    out, d = [], start
    while d <= end:
        out.append(d)
        d += relativedelta(months=months)
    return out


def flat_amortizer(coupon_dates: List[date], amort_map: Dict[date, float],
                   rate: float, freq: int, vr: float = 100.0) -> List[Dict]:
    """Cupón flat (saldo × tasa/freq) sobre saldo declinante + amort en montos
    EXPLÍCITOS (no cuotas iguales: ARCOR amortiza 33/33/34)."""
    cset = set(coupon_dates)
    out, outstanding = [], vr
    for d in sorted(cset | set(amort_map)):
        interest = outstanding * rate / freq if d in cset else 0.0
        amort = amort_map.get(d, 0.0)
        outstanding = max(outstanding - amort, 0.0)
        out.append({"date": d.isoformat(), "interest": round(interest, 6),
                    "amortization": round(amort, 6)})
    return out


# --- RC1CO: cupón semestral 31/01 y 31/07 (1er pago corrido a 02/02/2026 = lun);
#     amort 33/33/34 en 31/07/2031, 31/07/2032, 31/07/2033. vto 31/07/2033.
RC1CO_COUPONS = [date(2026, 2, 2)] + _grid(date(2026, 7, 31), date(2033, 7, 31), 6)
RC1CO_AMORT = {date(2031, 7, 31): 33.0, date(2032, 7, 31): 33.0, date(2033, 7, 31): 34.0}


BONDS = [
    {
        "tickers": ("RC1CO", "RC1CD", "RC1CC"), "isin": "USP04559BE29",
        "short": "ARCOR S.A.I.C. - Clase 1", "serie": "Clase 1",
        "cupon": "7.6", "freq": "2", "emision": date(2025, 7, 31), "vto": date(2033, 7, 31),
        "ley": "NY", "amort": "amortizing",
        "cashflows": flat_amortizer(RC1CO_COUPONS, RC1CO_AMORT, 0.076, 2),
        "verify_price": 106.0,
        "ref": {"tir": 7.07, "tna_nom": 6.95, "md": 4.72, "vt": 102.83,
                "accrued": 2.83, "clean": 103.171, "vr": 100.0, "parity": 103.08},
    },
    {
        "tickers": ("RAC7O", "RAC7D", "RAC7C"), "isin": "USP79849AF54",
        "short": "RAGHSA S.A. - Clase 7", "serie": "Clase 7",
        "cupon": "8.5", "freq": "2", "emision": date(2024, 12, 11), "vto": date(2032, 12, 11),
        "ley": "NY", "amort": "bullet", "cashflows": None,
        "verify_price": 103.0,
        "ref": {"tir": 8.07, "tna_nom": 7.91, "md": 4.94, "vt": 100.12,
                "accrued": 0.12, "clean": 102.882, "vr": 100.0, "parity": 102.88},
    },
    {
        "tickers": ("RAC5O", "RAC5D", "RAC5C"), "isin": "USP79849AE89",  # BYMADATA, no el del screenshot
        "short": "RAGHSA S.A. - Clase 5", "serie": "Clase 5",
        "cupon": "8.25", "freq": "2", "emision": date(2023, 4, 24), "vto": date(2030, 4, 24),
        "ley": "NY", "amort": "bullet", "cashflows": None,
        "verify_price": 102.0,
        "ref": {"tir": 8.15, "tna_nom": 7.99, "md": 3.22, "vt": 101.19,
                "accrued": 1.19, "clean": 100.808, "vr": 100.0, "parity": 100.80},
    },
    {
        "tickers": ("C138O", "C138D", "C138C"), "isin": "USP3063DAD41",
        "short": "CGC S.A. - Clase 38 (Adicionales)", "serie": "Clase 38 (Adic.)",
        "cupon": "11.875", "freq": "2", "emision": date(2025, 11, 28), "vto": date(2030, 11, 28),
        "ley": "NY", "amort": "bullet", "cashflows": None,
        "verify_price": 104.0,
        "ref": {"tir": 11.17, "tna_nom": 10.87, "md": 3.39, "vt": 100.59,
                "accrued": 0.59, "clean": 103.406, "vr": 100.0, "parity": 103.39},
    },
]


def _fields(b: dict) -> Dict[str, str]:
    o, d, c = b["tickers"]
    return {
        "ticker_ars": o, "ticker_mep": d, "ticker_ccl": c, "isin": b["isin"],
        "short_name": b["short"], "tipo": "HARD DOLLAR", "ley_aplicable": b["ley"],
        "serie_clase": b["serie"],
        "fecha_emision": b["emision"].isoformat(), "fecha_vencimiento": b["vto"].isoformat(),
        "cupon anual %": b["cupon"], "frecuencia pagos": b["freq"],
        "base calculo": "30/360", "tipo amortizacion": b["amort"],
    }


def _fmt(v: Optional[float], nd: int = 2) -> str:
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "—"


def main(dry_run: bool = False, force: bool = False) -> int:
    from apps.web.instruments_abm import _safe_synth

    print("== cashflows futuros (>settle 16/06/2026) ==")
    for b in BONDS:
        o = b["tickers"][0]
        if b["cashflows"] is None:
            cfs = [{"date": cf.date.isoformat(), "interest": cf.interest,
                    "amortization": cf.amortization} for cf in _safe_synth(_fields(b))]
            kind = "synth"
        else:
            cfs = b["cashflows"]
            kind = "explícito"
        fut = [x for x in cfs if x["date"] > "2026-06-16"]
        print(f"--- {o}  {b['short']}  [{kind}]  ({len(cfs)} flujos)")
        for x in fut[:2] + (["…"] if len(fut) > 3 else []) + fut[-1:]:
            if x == "…":
                print("      …")
            else:
                t = "  <- amort" if x["amortization"] else ""
                print(f"      {x['date']}  int={x['interest']:7.4f}  amort={x['amortization']:7.2f}{t}")
        print(f"      residual (Σ amort futura) = {sum(x['amortization'] for x in fut):.2f}")
    if dry_run:
        print("\n== DRY RUN (no escribe) ==")
        return 0

    from apps.web.instruments_abm import save_instrument, get_instrument
    from scripts.load_bond import verify

    if (rc := guard_write("pre-arcor-raghsa-cgc", force=force)):
        return rc
    for b in BONDS:
        pre = "ya existía" if get_instrument(b["tickers"][0]) else "nuevo"
        res = save_instrument(SHEET, _fields(b), cashflows=b["cashflows"])
        print(f"{res['action']:8} {', '.join(res['tickers'])}  ({b['short']}) [{pre}]")
    print()

    hdr = (f"{'ticker':6} {'TIR%':>7} {'TNAn%':>7} {'MD':>6} {'VT':>8} {'accr':>6} "
           f"{'clean':>9} {'VR':>7} {'parity%':>8}")
    print("VERIFICACIÓN  (engine → referencia)\n" + hdr)
    ok = True
    for b in BONDS:
        d_ticker = b["tickers"][1]
        r = verify(d_ticker, price=b["verify_price"], price_mode="dirty")
        if not r or r.get("error"):
            print(f"{d_ticker:6}  ERROR: {r.get('error') if r else 'sin resultado'}")
            ok = False
            continue
        ref = b["ref"]
        got = {"tir": (r.get("tir") or 0) * 100, "tna_nom": (r.get("tna_nominal") or 0) * 100,
               "md": r.get("duration"), "vt": r.get("technical_value"),
               "accrued": r.get("accrued_interest"), "clean": r.get("price_clean"),
               "vr": r.get("residual_nominal"), "parity": (r.get("parity") or 0) * 100}
        print(f"{d_ticker:6} {_fmt(got['tir']):>7} {_fmt(got['tna_nom']):>7} {_fmt(got['md']):>6} "
              f"{_fmt(got['vt']):>8} {_fmt(got['accrued']):>6} {_fmt(got['clean'],3):>9} "
              f"{_fmt(got['vr']):>7} {_fmt(got['parity']):>8}")
        print(f"{'  ref→':6} {ref['tir']:>7} {ref['tna_nom']:>7} {ref['md']:>6} "
              f"{ref['vt']:>8} {ref['accrued']:>6} {ref['clean']:>9.3f} {ref['vr']:>7} {ref['parity']:>8}")
        diffs = []
        for k, tol in (("tir", 0.4), ("md", 0.06), ("vt", 0.06), ("accrued", 0.06),
                       ("clean", 0.06), ("vr", 0.06), ("parity", 0.4)):
            g = got[k]
            if g is None or abs(g - ref[k]) > tol:
                diffs.append(f"{k} {_fmt(g, 3)}≠{ref[k]} (Δ{_fmt((g or 0)-ref[k],3)})")
        if diffs:
            ok = False
            print(f"       ⚠ {'; '.join(diffs)}")
        print()

    print("✓ las 4 ONs reconcilian con la referencia." if ok else
          "⚠ revisar divergencias arriba (ver nota RC1CO: 1er cupón corrido a día hábil).")
    print("\nReiniciá el server (o esperá el reload del repo) para verlas en el panel ON.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv,
                          force="--force" in sys.argv))
