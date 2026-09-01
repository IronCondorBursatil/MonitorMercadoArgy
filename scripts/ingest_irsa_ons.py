"""Alta de 4 ONs hard-dollar de IRSA (verificadas contra la calculadora de referencia,
   pestaña "Condiciones de Emisión"):

    IRCJO  IRSA 2027 u$s 7%      (AR0961375257)  regular bullet  → synth
    IRCLO  IRSA 2026 u$s 6%      (AR0200915251)  regular bullet  → synth   (¡ya vencida!)
    IRCNO  IRSA 2027 u$s 5,75%   (AR0275431465)  schedule irregular → cashflows explícitos
    IRCOO  IRSA 2029 u$s 7,25%   (AR0084572384)  schedule irregular → cashflows explícitos

Por qué este script y NO la CSV / re-ingest:
  - El catálogo VIVO es SQLite y tiene 69 ONs "DB-only" (altas a mano vía ABM, p.ej.
    BACHO/CLISA/YM42) que NO están en `data/obligaciones_negociables.csv`. Correr
    `on_catalog.ingest()` BORRA toda la hoja y re-siembra desde la CSV → perdería esas
    69. Por eso damos de alta vía `save_instrument` (append, no destructivo) — el mismo
    camino que usa la ABM. Estas 4 quedan DB-only, igual que las otras 69.

Por qué IRCNO/IRCOO con cashflows explícitos:
  - Tienen 1er cupón LARGO (emisión 23/10/2024 → 1er cupón 23/07/2025, ~9m) y cupón
    final CORTO/stub (23/07 → 23/10, ~3m). `synth_cashflows` day-weightea ACT/365 OK,
    pero su lógica "long last coupon" FUSIONA el stub final con el vencimiento (paga 1
    cupón en vez de 2) — es el caso YM42 de `tests/test_golden_referencia.py`, que por eso
    también usa cashflows explícitos. Los regulares (IRCJO/IRCLO) sí los sintetiza bien.

Idempotente (`save_instrument` actualiza si ya existe). Snapshot pre-op incondicional.

    py -3.12 scripts/ingest_irsa_ons.py            # da de alta + verifica
    py -3.12 scripts/ingest_irsa_ons.py --dry-run  # solo muestra los cashflows, no escribe
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EMISOR = "IRSA INVERSIONES Y REPRESENTACIONES S.A."
SHEET = "Obligaciones_Negociables"


def act365_bullet(emission: date, coupon_dates: list[date], rate: float,
                  vr: float = 100.0) -> list[dict]:
    """Cashflows de un bono bullet con cupón ACT/365 day-weighted: cada cupón =
    VR × rate × días_del_período / 365 (saldo constante hasta el vto). La
    amortización (VR) va en la última fecha. `coupon_dates` son las fechas de
    devengamiento (columna "Vencimiento" del calculador), terminando en el vto."""
    out: list[dict] = []
    prev = emission
    last = coupon_dates[-1]
    for d in coupon_dates:
        interest = vr * rate * (d - prev).days / 365.0
        amort = vr if d == last else 0.0
        out.append({"date": d.isoformat(), "interest": round(interest, 6),
                    "amortization": round(amort, 6)})
        prev = d
    return out


# --- los 4 bonos -----------------------------------------------------------
# regular bullet (synth) → cashflows=None; irregular → cashflows explícitos.
BONDS = [
    {
        "tickers": ("IRCJO", "IRCJD", "IRCJC"), "isin": "AR0961375257",
        "name": "IRSA 2027 u$s 7%", "cupon": "7", "emision": date(2024, 2, 28),
        "vto": date(2027, 2, 28), "cashflows": None,  # grilla Feb-28/Aug-28, regular
    },
    # IRCLO (IRSA 2026 u$s 6%, AR0200915251) se OMITE: venció el 10/06/2026 → sin
    # flujos futuros / sin TIR. Se dio de alta y se borró el 2026-06-14. No re-cargar.
    {
        "tickers": ("IRCNO", "IRCND", "IRCNC"), "isin": "AR0275431465",
        "name": "IRSA 2027 u$s 5,75%", "cupon": "5.75", "emision": date(2024, 10, 23),
        "vto": date(2027, 10, 23), "prox_cupon": date(2025, 7, 23),
        "coupon_dates": [date(2025, 7, 23), date(2026, 1, 23), date(2026, 7, 23),
                         date(2027, 1, 23), date(2027, 7, 23), date(2027, 10, 23)],
        "rate": 0.0575,
    },
    {
        "tickers": ("IRCOO", "IRCOD", "IRCOC"), "isin": "AR0084572384",
        "name": "IRSA 2029 u$s 7,25%", "cupon": "7.25", "emision": date(2024, 10, 23),
        "vto": date(2029, 10, 23), "prox_cupon": date(2025, 7, 23),
        "coupon_dates": [date(2025, 7, 23), date(2026, 1, 23), date(2026, 7, 23),
                         date(2027, 1, 23), date(2027, 7, 23), date(2028, 1, 23),
                         date(2028, 7, 23), date(2029, 1, 23), date(2029, 7, 23),
                         date(2029, 10, 23)],
        "rate": 0.0725,
    },
]


def _fields(b: dict) -> dict:
    o, d, c = b["tickers"]
    f = {
        "ticker_ars": o, "ticker_mep": d, "ticker_ccl": c, "isin": b["isin"],
        "short_name": EMISOR, "tipo": "HARD DOLLAR", "ley_aplicable": "Argentina",
        "fecha_emision": b["emision"].isoformat(),
        "fecha_vencimiento": b["vto"].isoformat(),
        "cupon anual %": b["cupon"], "frecuencia pagos": "2",
        "base calculo": "ACT/365",  # crítico: vacío → synth usa cupón plano (no day-weighted)
        "tipo amortizacion": "bullet",
        "denom_base": "1.00", "denom_incremento": "1.00", "valor_nominal": "1.00",
    }
    if b.get("prox_cupon"):
        f["prox_cupon"] = b["prox_cupon"].isoformat()
    return f


def _explicit_cfs(b: dict) -> list[dict] | None:
    if b.get("cashflows") is not None or "coupon_dates" not in b:
        return None
    return act365_bullet(b["emision"], b["coupon_dates"], b["rate"])


def main(dry_run: bool) -> None:
    from core.domain.models import Cashflow, Instrument
    from core.domain.pricing.base import VanillaStrategy
    from core.domain.pricing.context import PricingContext
    from core.domain.pricing import metrics

    strat = VanillaStrategy()
    settle = date(2026, 6, 15)  # T+1 hábil aprox. (solo para el sanity check de TIR)

    if dry_run:
        print("== DRY RUN (no escribe) ==\n")
    else:
        from config.settings import settings
        from core.infrastructure.db.backup import backup_db
        snap = backup_db(settings.catalog_db, settings.backup_dir,
                         keep=settings.backup_keep, tag="pre-irsa-ons")
        print(f"backup pre-op: {snap}\n")
        from apps.web.instruments_abm import save_instrument

    for b in BONDS:
        o = b["tickers"][0]
        fields = _fields(b)
        cfs_explicit = _explicit_cfs(b)
        kind = "explícito" if cfs_explicit is not None else "synth"
        print(f"--- {o}  {b['name']}  [{kind}]  ISIN {b['isin']}")

        if not dry_run:
            res = save_instrument(SHEET, fields, cashflows=cfs_explicit)
            print(f"    {res['action']}: {', '.join(res['tickers'])}")

        # --- reconstruir el Instrument para verificar el schedule + TIR ---
        if cfs_explicit is not None:
            cf_objs = [Cashflow(date=date.fromisoformat(x["date"]),
                                amortization=x["amortization"], interest=x["interest"])
                       for x in cfs_explicit]
        else:
            from core.domain.cashflow_synth import synth_cashflows
            norm = {k.lower(): v for k, v in fields.items()}
            cf_objs = list(synth_cashflows(norm))

        inst = Instrument(
            ticker=b["tickers"][1], short_name=f"{EMISOR} - {b['name']}",
            instrument_type="HARD DOLLAR", maturity_date=b["vto"],
            emission_date=b["emision"], payment_frequency=2, day_count="ACT/365",
            category="Obligaciones Negociables", cashflows=tuple(cf_objs),
        )
        for cf in cf_objs:
            tag = "  <- AMORT+stub" if (cf.amortization and cf.interest and cf.date == b["vto"]) else ""
            print(f"      {cf.date}  int={cf.interest:8.4f}  amort={cf.amortization:7.2f}{tag}")

        # sanity: TIR del leg D a dirty=100 (debería ≈ cupón para un bullet a la par)
        future = [cf for cf in cf_objs if cf.date > settle]
        if not future:
            print(f"      TIR @100: n/a (VENCIDO al {settle} — sin flujos futuros)")
        else:
            ctx = PricingContext(settle=settle)
            tir = strat.tir(inst, 100.0, ctx)
            ai = metrics.accrued_interest(inst, settle)
            print(f"      TIR @dirty 100: {tir*100:5.2f}%   accrued={ai:.4f}   "
                  f"(cupón {b['cupon']}%)")
        print()

    if not dry_run:
        print("Listo. Reiniciá el server (o esperá el reload del repo) para verlas en el panel.")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
