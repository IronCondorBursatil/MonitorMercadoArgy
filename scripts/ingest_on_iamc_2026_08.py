"""Alta de las ON del informe IAMC "Deuda Corporativa" 28-Aug-26 (EXCLUYE las TAMAR en pesos).

Todas van a la hoja Obligaciones_Negociables (panel ON), con los tipos que ya usa
el catálogo: "HARD DOLLAR" (paga USD) y "DOLLAR LINKED" (emitida en USD, paga ARS).

CRONOGRAMA: para un bullet a tasa fija el schedule queda DETERMINADO por
(emisión, vencimiento, tasa, frecuencia) — no hace falta la ficha BYMA:
grilla de cupones regulares hacia atrás desde el vencimiento + amortización única
del VR al vto. Los AMORTIZABLES (cuotas de capital > 1) NO se cargan acá: el
informe no publica el cronograma de capital y sin él la TIR saldría falsa.

VALIDACIÓN (criterio de aceptación, mismo método que ingest_iamc_2026_08):
se compara contra lo que PUBLICA el IAMC a la liquidación 31/08/2026 —
intereses corridos, V.Téc y TIR. El bono que no reproduce esos números NO se carga.

    py -3.12 scripts/ingest_on_iamc_2026_08.py --dry-run
    py -3.12 scripts/ingest_on_iamc_2026_08.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scratch"))

from dateutil.relativedelta import relativedelta  # noqa: E402

from core.domain.models import Cashflow, Instrument, MarketSnapshot  # noqa: E402
from core.domain.pricing import metrics  # noqa: E402
from core.domain.services import FinancialEngine  # noqa: E402

SETTLE = date(2026, 8, 31)
SHEET = "Obligaciones_Negociables"

# FX implícito del informe (cierre_ars / precio_usd de los globales donde IAMC publica
# ambos: AL30 1535.27, GD30 1535.26, AE38 1535.29). Las ON cotizan en pesos y la
# strategy las pasa a USD por MEP (ley AR) / CCL (ley EXT).
_FX = 1535.27


class _StubFx:
    def get_mep_venta(self):
        return _FX

    def get_ccl_venta(self):
        return _FX

    def get_mayorista_venta(self):
        return _FX


def _d(s) -> date:
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


# "Próximo Cupón" publicado por el IAMC para los bonos cuya grilla NO cae en el
# aniversario del vencimiento (primer período irregular). Ancla la grilla en la fecha
# REAL de pago → sin esto el accrued sale mal y la V.Téc no reconcilia.
PROX_CUPON = {
    "AERBO": "2026-12-15", "BYCWO": "2026-11-30", "BYCXO": "2026-12-30",
    "CS50O": "2026-09-10", "EAC1O": "2026-10-12", "IRCNO": "2027-01-23",
    "IRCOO": "2027-01-23", "LDCGO": "2026-11-04", "OTS6O": "2026-10-29",
    "PLC1O": "2026-10-27", "PLC2O": "2026-10-27", "PLC3O": "2027-01-30",
    "PLC6O": "2026-11-27", "WBS3O": "2026-09-30", "YFCOO": "2026-09-15",
    "YM42O": "2026-12-02",
}


def build_cashflows(row: dict):
    """Bullet a tasa fija: grilla de cupones regulares + amortización del VR al vto.

    La grilla se ancla en el PRÓXIMO CUPÓN publicado cuando se conoce (fecha de pago
    real); si no, en el vencimiento (que es lo habitual: la grilla cae en su
    aniversario). Se extiende hacia atrás hasta la emisión y hacia adelante hasta el
    vto. Cupón por período = tasa_anual / frecuencia sobre el VR."""
    emision, vto = _d(row["emision"]), _d(row["vto"])
    vr, freq, tasa = row["vr"], row["frec"], row["tasa"]

    if freq == 0:                                   # cupón cero
        return [Cashflow(date=vto, amortization=vr, interest=0.0)]

    meses = 12 // freq
    cupon = (tasa / 100.0) * vr / freq              # monto por período, per VR
    ancla = _d(PROX_CUPON[row["ticker"]]) if row["ticker"] in PROX_CUPON else vto

    fechas = []
    d = ancla                                       # hacia atrás hasta la emisión
    while d > emision:
        fechas.append(d)
        d = d - relativedelta(months=meses)
    d = ancla + relativedelta(months=meses)         # hacia adelante hasta el vto
    while d < vto:
        fechas.append(d)
        d = d + relativedelta(months=meses)
    if vto not in fechas:
        fechas.append(vto)                          # el vto siempre paga
    fechas = sorted(set(f for f in fechas if f > emision))
    return [Cashflow(date=f, amortization=(vr if f == vto else 0.0), interest=cupon)
            for f in fechas]


def build_instrument(row: dict) -> Instrument:
    tipo = "DOLLAR LINKED" if row["tipo"] in ("DL", "ZC") else "HARD DOLLAR"
    return Instrument(
        ticker=row["ticker"], short_name=row.get("emisor") or row["ticker"],
        instrument_type=tipo,
        maturity_date=_d(row["vto"]), emission_date=_d(row["emision"]),
        payment_frequency=max(row["frec"], 1),
        day_count="ACT/365",                        # convención ON del catálogo
        cashflows=tuple(build_cashflows(row)),
        ley_aplicable=("Argentina" if row["ley"] == "AR" else "Extranjera"),
    )


def validar(row: dict) -> dict:
    """Compara accrued / V.Téc / TIR contra lo publicado. Tolerancia de TIR relativa
    (1%) en yields extremos: con paridad publicada a 1 decimal, un bono que rinde
    100%+ mueve decenas de bp por el solo redondeo del precio."""
    inst = build_instrument(row)
    out = {"ticker": row["ticker"], "fallas": []}

    accr = metrics.accrued_interest(inst, SETTLE)
    out["accr"] = accr
    if abs(accr - row["accr"]) > 0.12:
        out["fallas"].append("accr %.3f != %.2f" % (accr, row["accr"]))

    vr = sum(c.amortization for c in inst.cashflows if c.date > SETTLE)
    vt = vr + accr
    out["vt"] = vt
    if abs(vt - row["vt"]) > 0.20:
        out["fallas"].append("VT %.3f != %.2f" % (vt, row["vt"]))

    # TIR con el FX IMPLÍCITO de ESTE bono en el informe. El IAMC valúa unas ON al
    # MEP (~1535) y otras al CCL (~1603) — 4,4% de spread. Ese es un input de PRECIO
    # del día, no una propiedad del cronograma: usar un FX único haría "fallar" bonos
    # cuyo schedule es correcto. Se exige que el implícito caiga en la banda MEP–CCL
    # plausible; si se fuera de ahí, algo del bono no cierra y se rechaza.
    px_usd = row["paridad"] * row["vt"] / 100.0
    fx_impl = row["cierre_ars"] / px_usd if px_usd > 0 else None
    out["fx_impl"] = fx_impl
    if fx_impl is None or not (1450.0 <= fx_impl <= 1700.0):
        out["fallas"].append("FX implícito fuera de banda MEP-CCL: %s" % (
            "%.1f" % fx_impl if fx_impl else "n/d"))
    else:
        class _Fx:
            def get_mep_venta(self):
                return fx_impl

            def get_ccl_venta(self):
                return fx_impl

            def get_mayorista_venta(self):
                return fx_impl

        tir = FinancialEngine.calculate_tir(
            MarketSnapshot(instrument=inst, price=row["cierre_ars"]),
            settle_date=SETTLE, fx_provider=_Fx())
        out["tir"] = tir * 100.0 if tir is not None else None
        tol = max(0.25, 0.01 * abs(row["ytm"]))
        if out["tir"] is None:
            out["fallas"].append("TIR None (esperado %.2f)" % row["ytm"])
        elif abs(out["tir"] - row["ytm"]) > tol:
            out["fallas"].append("TIR %.4f != %.2f (tol %.2f)" % (out["tir"], row["ytm"], tol))

    out["ok"] = not out["fallas"]
    return out


def main(dry_run: bool = False) -> int:
    from on_iamc_data import ONS

    bullets = {t: r for t, r in ONS.items() if r["bullet"]}
    amort = sorted(t for t, r in ONS.items() if not r["bullet"])
    print("transcriptos %d  ->  bullets %d | amortizables %d (no se cargan)\n"
          % (len(ONS), len(bullets), len(amort)))

    ok, fail = {}, []
    for t, row in sorted(bullets.items()):
        r = validar(row)
        if r["ok"]:
            ok[t] = row
        else:
            fail.append((t, "; ".join(r["fallas"])))

    print("VALIDAN contra el IAMC: %d/%d" % (len(ok), len(bullets)))
    if fail:
        print("\nNO validan (%d, no se cargan):" % len(fail))
        for t, det in fail:
            print("  %-7s %s" % (t, det))
    print("\nAmortizables sin cronograma publicado (%d): %s" % (len(amort), " ".join(amort)))

    if dry_run:
        print("\n== DRY RUN (no escribe) ==")
        return 0
    if not ok:
        print("\nNada que cargar.")
        return 1

    from config.settings import settings
    from core.infrastructure.db.backup import backup_db
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import CashflowORM, InstrumentORM

    snap = backup_db(settings.catalog_db, settings.backup_dir,
                     keep=settings.backup_keep, tag="pre-on-iamc")
    print("\nbackup pre-op: %s" % snap)

    init_db()
    creados, actualizados = [], []
    with SessionLocal.begin() as s:
        for t, row in sorted(ok.items()):
            inst = build_instrument(row)
            orm = s.get(InstrumentORM, t)
            if orm is None:
                orm = InstrumentORM(ticker=t)
                s.add(orm)
                creados.append(t)
            else:
                actualizados.append(t)
            orm.short_name = row.get("emisor") or t
            orm.instrument_type = inst.instrument_type
            orm.sheet = SHEET
            orm.maturity_date = inst.maturity_date
            orm.emission_date = inst.emission_date
            orm.payment_frequency = inst.payment_frequency
            orm.day_count = inst.day_count
            orm.category = "Obligaciones Negociables"
            orm.raw_fields = {"origen": "IAMC 2026-08-28 (deuda corporativa)",
                              "ley_aplicable": inst.ley_aplicable,
                              "cupon_anual_pct": row["tasa"]}
            orm.cashflows = [
                CashflowORM(ticker=t, fecha_pago=c.date,
                            amortizacion=c.amortization, cupon_interes=c.interest)
                for c in inst.cashflows
            ]

    print("\ncreados: %d | actualizados: %d" % (len(creados), len(actualizados)))
    from core.infrastructure.db.catalog_repository import CatalogRepository
    en_db = {i.ticker for i in CatalogRepository(auto_seed=False).get_all_instruments()}
    faltan = [t for t in ok if t not in en_db]
    print("verificación: %d/%d en la DB" % (len(ok) - len(faltan), len(ok)))
    if faltan:
        print("  NO quedaron: " + " ".join(faltan))
        return 1
    print("\nOK. Reiniciá el server para verlas en el panel ON.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv))
