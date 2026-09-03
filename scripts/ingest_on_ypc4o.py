"""Alta de YPC4O — YPF 2028 u$s 10% (Internacional), ON hard-dollar BULLET.

    YPC4O / YPC4D / YPC4C   ISIN US984245AF78   Ley NY   10% TNA semestral, US 30/360
    dated 02/11/1998 → vto 02/11/2028   bullet (amortiza 100 al vto)

Bullet regular → entra por ABM (synth), cashflows=None. NO explícito (política: synth
para bullet/ZC; explícito sólo amortizing/irregular).

Gotcha del dated-date: la "Vigencia 12/11/1998" del informe es la fecha de EMISIÓN/
liquidación, pero los cupones se anclan al DÍA 02 (02/05 y 02/11, = día del vto). El
synth ancla la grilla a `fecha_emision` y avanza de a 6m; con 12/11 caería en el día 12
y, peor, el último cupón (12/05/2028) < vto activaría el "long last coupon" fusionándolo
con el vto → se PERDERÍA un cupón. Usando el dated-date real 02/11/1998 la grilla cae
en 60 cupones exactos terminando en el vto (02/11/2028), bullet limpio. La pequeña
divergencia vs la referencia (cupones flat 5.00 vs 4.94/5.03/4.97 por el ajuste de día hábil
del calculador) es la aproximación normal del synth para un bullet.

DB-only (save_instrument append, NO destructivo). Snapshot pre-op. Idempotente.

    py -3.12 scripts/ingest_on_ypc4o.py            # alta + verificación
    py -3.12 scripts/ingest_on_ypc4o.py --dry-run  # sólo mira la grilla synth
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.op_guards import guard_write  # noqa: E402

SHEET = "Obligaciones_Negociables"

FIELDS = {
    "ticker_ars": "YPC4O", "ticker_mep": "YPC4D", "ticker_ccl": "YPC4C",
    "isin": "US984245AF78", "short_name": "YPF S.A. - YPF 2028 u$s 10%",
    "serie_clase": "Internacional", "tipo": "HARD DOLLAR", "ley_aplicable": "Extranjera",   # ley NY -> pata pesos contra CCL
    "fecha_emision": "1998-11-02",        # dated-date (ancla la grilla al día 02), no el 12/11 del informe
    "fecha_vencimiento": "2028-11-02",
    "cupon anual %": "10", "frecuencia pagos": "2",
    "base calculo": "30/360", "tipo amortizacion": "bullet",
}

# la referencia (simular inversión, dirty 100, T+1 → 16/06/2026)
REF = {"tir": 10.81, "tna_nom": 10.53, "md": 1.95, "vt": 101.1667,
       "clean": 98.83, "vr": 100.0}
VERIFY_PRICE = 100.0


def _fmt(v, nd=2):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "—"


def main(dry_run: bool = False, force: bool = False) -> int:
    from apps.web.instruments_abm import _safe_synth

    synth = _safe_synth(FIELDS)
    fut = [cf for cf in synth if cf.date.isoformat() > "2026-06-16"]
    print(f"grilla synth: {len(synth)} cupones; futuros (>settle):")
    for cf in fut:
        tag = "  <- cupón+amort (bullet)" if cf.amortization else ""
        print(f"  {cf.date}  int={cf.interest:7.4f}  amort={cf.amortization:7.2f}{tag}")
    print()
    if dry_run:
        print("== DRY RUN (no escribe) ==")
        return 0

    from apps.web.instruments_abm import save_instrument
    from scripts.load_bond import verify

    if (rc := guard_write("pre-ypc4o", force=force)):
        return rc
    res = save_instrument(SHEET, FIELDS, cashflows=None)  # bullet → synth
    print(f"{res['action']}: {', '.join(res['tickers'])}\n")

    r = verify("YPC4D", price=VERIFY_PRICE, price_mode="dirty")
    if not r or r.get("error"):
        print(f"ERROR verify: {r.get('error') if r else 'sin resultado'}")
        return 1
    got = {"tir": (r.get("tir") or 0) * 100, "tna_nom": (r.get("tna_nominal") or 0) * 100,
           "md": r.get("duration"), "vt": r.get("technical_value"),
           "clean": r.get("price_clean"), "vr": r.get("residual_nominal")}
    print(f"{'':6} {'TIR%':>7} {'TNAn%':>7} {'MD':>6} {'VT':>9} {'clean':>8} {'VR':>7}")
    print(f"{'engine':6} {_fmt(got['tir']):>7} {_fmt(got['tna_nom']):>7} {_fmt(got['md']):>6} "
          f"{_fmt(got['vt'],4):>9} {_fmt(got['clean']):>8} {_fmt(got['vr']):>7}")
    print(f"{'Ref':6} {REF['tir']:>7} {REF['tna_nom']:>7} {REF['md']:>6} "
          f"{REF['vt']:>9.4f} {REF['clean']:>8} {REF['vr']:>7}")
    diffs = []
    for k, tol in (("tir", 0.25), ("md", 0.05), ("vt", 0.1), ("clean", 0.1), ("vr", 0.05)):
        g = got[k]
        if g is None or abs(g - REF[k]) > tol:
            diffs.append(f"{k} {_fmt(g,4)}≠{REF[k]} (tol {tol})")
    print("\n" + ("✓ YPC4O reconcilia con la referencia." if not diffs
                  else "⚠ diverge: " + "; ".join(diffs)))
    print("Reiniciá el server (o esperá el reload del repo) para verla en el panel ON.")
    return 0 if not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv,
                          force="--force" in sys.argv))
