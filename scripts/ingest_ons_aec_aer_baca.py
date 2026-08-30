"""Alta de 4 ONs hard-dollar (verificadas contra la calculadora de Balanz, T+1 al
2026-06-16, con el feriado/hábil del repo):

    AEC2O  AES Argentina Gen. Clase 2   9.500%  USP1000CAE41  amort 25%×4    → explícito
    AER1O  Aeropuertos Arg. Clase 2020  6.875%  USP0092MAF07  amort 4.17%×24 → explícito
    AERAO  Aeropuertos Arg. 2000 v2027  6.875%  USP0092MAE32  amort 3.13%×32 → explícito
    BACAO  Banco Macro Clase A          6.643%  USP1047VAF42  bullet         → synth

Por qué explícitos los 3 amortizers (política del repo: synth SOLO para bullet/ZC):
  El cupón flat (saldo × tasa/freq) sobre saldo declinante + amortización en cuotas
  iguales reproduce EXACTO los valores de hoy (TIR/MD/VT/accrued). Los cupones
  irregulares del arranque (1er período 2017/2020) son pasado: no inciden en el precio
  (el motor sólo descuenta flujos > settle) y sólo afectarían filas históricas del
  display. build_on_cashflows NO sirve acá: su grilla por relativedelta clampea el
  30/08 ↔ 28/02 de AEC2O (Feb→28) y agrega un stub espurio. Por eso dates explícitas.

BACAO es bullet con UN flujo futuro (04/11/2026 = cupón 3.3215 + amort 100) → synth ABM
con base 30/360 (cupón flat 6.643%/2 = 3.3215, matchea Balanz).

DB-only (save_instrument append, NO destructivo) — igual que las ~69 ONs cargadas a mano;
NO toca la CSV semilla ni on_catalog.ingest (destructivo). Snapshot pre-op incondicional.
Idempotente (save_instrument actualiza si ya existe).

    py -3.12 scripts/ingest_ons_aec_aer_baca.py            # alta + verificación
    py -3.12 scripts/ingest_ons_aec_aer_baca.py --dry-run  # sólo arma/mira cashflows
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from dateutil.relativedelta import relativedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SHEET = "Obligaciones_Negociables"


# --------------------------------------------------------------------------- #
# Cashflows explícitos: cupón flat (saldo × tasa/freq) + amort en cuotas iguales.
# Idéntico a core.domain.on_cashflows.build_on_cashflows pero con DATES explícitas
# (sin clamping de relativedelta). `coupon_dates`/`amort_dates` salen de la imagen.
# --------------------------------------------------------------------------- #
def _grid(start: date, end: date, months: int) -> List[date]:
    """Fechas [start, end] paso `months` (día fijo → sin clamping si day ≤ 28)."""
    out, d = [], start
    while d <= end:
        out.append(d)
        d += relativedelta(months=months)
    return out


def flat_amortizer(coupon_dates: List[date], amort_dates: List[date],
                   rate: float, freq: int, vr: float = 100.0) -> List[Dict]:
    per = vr / len(amort_dates)
    amort_map = {d: per for d in amort_dates}
    cset = set(coupon_dates)
    out, outstanding = [], vr
    for d in sorted(cset | set(amort_map)):
        interest = outstanding * rate / freq if d in cset else 0.0
        amort = amort_map.get(d, 0.0)
        outstanding = max(outstanding - amort, 0.0)
        out.append({"date": d.isoformat(), "interest": round(interest, 6),
                    "amortization": round(amort, 6)})
    return out


# --- AEC2O: 9.5% semestral, 30/360. Cupón 30/08 y 28-29/02; amort 25% en los 4
#     últimos cupones (28/02/26, 30/08/26, 28/02/27, 30/08/27). vto 30/08/2027.
AEC2O_COUPONS = [date(2024, 2, 29), date(2024, 8, 30), date(2025, 2, 28),
                 date(2025, 8, 30), date(2026, 2, 28), date(2026, 8, 30),
                 date(2027, 2, 28), date(2027, 8, 30)]
AEC2O_AMORT = [date(2026, 2, 28), date(2026, 8, 30), date(2027, 2, 28), date(2027, 8, 30)]

# --- AER1O: 6.875% trim, 30/360. Cupón 01/02-05-08-11 desde 01/08/2020; amort
#     4.1667% (=100/24) trim desde 01/05/2021. vto 01/02/2027.
AER1O_COUPONS = _grid(date(2020, 8, 1), date(2027, 2, 1), 3)
AER1O_AMORT = _grid(date(2021, 5, 1), date(2027, 2, 1), 3)

# --- AERAO: 6.875% trim, 30/360. Cupón desde 01/05/2017; amort 3.125% (=100/32)
#     trim desde 01/05/2019. vto 01/02/2027.
AERAO_COUPONS = _grid(date(2017, 5, 1), date(2027, 2, 1), 3)
AERAO_AMORT = _grid(date(2019, 5, 1), date(2027, 2, 1), 3)


BONDS = [
    {
        "tickers": ("AEC2O", "AEC2D", "AEC2C"), "isin": "USP1000CAE41",
        "short": "AES ARGENTINA GENERACION - Clase 2", "serie": "Clase 2",
        "cupon": "9.5", "freq": "2", "emision": date(2023, 8, 30), "vto": date(2027, 8, 30),
        "ley": "NY", "amort": "amortizing",
        "cashflows": flat_amortizer(AEC2O_COUPONS, AEC2O_AMORT, 0.095, 2),
        "verify_price": 70.0,
        "ref": {"tir": 26.89, "tna_nom": 25.27, "md": 0.57, "vt": 77.10,
                "accrued": 2.10, "clean": 67.90, "vr": 75.0, "parity": 90.79},
    },
    {
        "tickers": ("AER1O", "AER1D", "AER1C"), "isin": "USP0092MAF07",
        "short": "AEROPUERTOS ARGENTINA 2000 - Clase 2020", "serie": "Clase 2020",
        "cupon": "6.875", "freq": "4", "emision": date(2020, 5, 20), "vto": date(2027, 2, 1),
        "ley": "NY", "amort": "amortizing",
        "cashflows": flat_amortizer(AER1O_COUPONS, AER1O_AMORT, 0.06875, 4),
        "verify_price": 12.0,
        "ref": {"tir": 22.26, "tna_nom": 20.60, "md": 0.35, "vt": 12.61,
                "accrued": 0.11, "clean": 11.89, "vr": 12.50, "parity": 95.19},
    },
    {
        "tickers": ("AERAO", "AERAD", "AERAC"), "isin": "USP0092MAE32",
        "short": "AEROPUERTOS ARGENTINA 2000 - Vto. 2027", "serie": "Vto. 2027",
        "cupon": "6.875", "freq": "4", "emision": date(2017, 2, 6), "vto": date(2027, 2, 1),
        "ley": "NY", "amort": "amortizing",   # ⚠ el calculador muestra Ley ARG; user pidió NY
        "cashflows": flat_amortizer(AERAO_COUPONS, AERAO_AMORT, 0.06875, 4),
        "verify_price": 9.0,
        "ref": {"tir": 22.28, "tna_nom": 20.62, "md": 0.35, "vt": 9.46,
                "accrued": 0.08, "clean": 8.92, "vr": 9.38, "parity": 95.18},
    },
    {
        "tickers": ("BACAO", "BACAD", "BACAC"), "isin": "USP1047VAF42",
        "short": "BANCO MACRO - Clase A", "serie": "Clase A",
        "cupon": "6.643", "freq": "2", "emision": date(2016, 11, 4), "vto": date(2026, 11, 4),
        "ley": "NY", "amort": "bullet",
        "cashflows": None,   # bullet → synth
        "verify_price": 101.20,
        "ref": {"tir": 5.52, "tna_nom": 5.44, "md": 0.38, "vt": 100.78,
                "accrued": 0.78, "clean": 100.425, "vr": 100.0, "parity": 100.42},
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


def main(dry_run: bool) -> int:
    if dry_run:
        print("== DRY RUN (no escribe) ==\n")
        for b in BONDS:
            o = b["tickers"][0]
            cfs = b["cashflows"]
            print(f"--- {o}  {b['short']}  [{'synth' if cfs is None else 'explícito'}]")
            if cfs is None:
                print("    (synth bullet en el alta real)")
            else:
                fut = [x for x in cfs if x["date"] > "2026-06-16"]
                print(f"    {len(cfs)} flujos; futuros (>settle):")
                for x in fut:
                    print(f"      {x['date']}  int={x['interest']:8.4f}  amort={x['amortization']:8.4f}")
                print(f"    residual (Σ amort futura) = {sum(x['amortization'] for x in fut):.4f}")
        return 0

    from config.settings import settings
    from core.infrastructure.db.backup import backup_db
    from scripts.load_bond import verify
    from apps.web.instruments_abm import save_instrument

    snap = backup_db(settings.catalog_db, settings.backup_dir,
                     keep=settings.backup_keep, tag="pre-aec-aer-baca")
    print(f"backup pre-op: {snap}\n")

    for b in BONDS:
        res = save_instrument(SHEET, _fields(b), cashflows=b["cashflows"])
        print(f"{res['action']:8} {', '.join(res['tickers'])}  ({b['short']})")
    print()

    # --- Verificación contra Balanz (leg …D, USD, dirty, T+1) ---
    hdr = f"{'ticker':6} {'TIR%':>7} {'TNAn%':>7} {'MD':>6} {'VT':>8} {'accr':>6} {'clean':>9} {'VR':>7} {'parity%':>8}"
    print("VERIFICACIÓN  (engine → Balanz)\n" + hdr)
    ok = True
    for b in BONDS:
        d_ticker = b["tickers"][1]
        r = verify(d_ticker, price=b["verify_price"], price_mode="dirty")
        if not r or r.get("error"):
            print(f"{d_ticker:6}  ERROR: {r.get('error') if r else 'sin resultado'}")
            ok = False
            continue
        ref = b["ref"]
        got = {
            "tir": (r.get("tir") or 0) * 100, "tna_nom": (r.get("tna_nominal") or 0) * 100,
            "md": r.get("duration"), "vt": r.get("technical_value"),
            "accrued": r.get("accrued_interest"), "clean": r.get("price_clean"),
            "vr": r.get("residual_nominal"), "parity": (r.get("parity") or 0) * 100,
        }
        print(f"{d_ticker:6} {_fmt(got['tir']):>7} {_fmt(got['tna_nom']):>7} {_fmt(got['md']):>6} "
              f"{_fmt(got['vt']):>8} {_fmt(got['accrued']):>6} {_fmt(got['clean'],4):>9} "
              f"{_fmt(got['vr']):>7} {_fmt(got['parity']):>8}")
        print(f"{'  ref→':6} {ref['tir']:>7} {ref['tna_nom']:>7} {ref['md']:>6} "
              f"{ref['vt']:>8} {ref['accrued']:>6} {ref['clean']:>9.4f} {ref['vr']:>7} {ref['parity']:>8}")
        # tolerancias: TIR ±0.4pp, MD ±0.03, VT/accrued/clean/VR ±0.06, parity ±0.4
        diffs = []
        for k, tol in (("tir", 0.4), ("md", 0.03), ("vt", 0.06), ("accrued", 0.06),
                       ("clean", 0.06), ("vr", 0.06), ("parity", 0.4)):
            g = got[k]
            if g is None or abs(g - ref[k]) > tol:
                diffs.append(f"{k} {_fmt(g, 4)}≠{ref[k]} (tol {tol})")
        if diffs:
            ok = False
            print(f"       ⚠ DIVERGE: {'; '.join(diffs)}")
        print()

    print("✓ las 4 ONs reconcilian con Balanz." if ok else
          "⚠ alguna métrica diverge — revisar arriba.")
    print("\nReiniciá el server (o esperá el reload del repo) para verlas en el panel ON.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv))
