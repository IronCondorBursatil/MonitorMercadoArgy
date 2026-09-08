"""Alta de TMVE8 (DUAL_DL_TAMAR) y PR17 (PROVINCIAL ARS) en el catálogo — 2026-09-08.

    py -3.12 scripts/alta_tmve8_pr17.py                 # DRY RUN (default): muestra el plan
    py -3.12 scripts/alta_tmve8_pr17.py --apply         # escribe (backup pre-op; aborta con server vivo)
    py -3.12 scripts/alta_tmve8_pr17.py --apply --force # con el server vivo (reiniciar después: el
                                                        # repo cachea el catálogo en memoria)
    py -3.12 scripts/alta_tmve8_pr17.py --verify        # TIR / V.Téc de los dos con providers reales

En prod: `MONITOR_DB_DIR=/var/lib/monitor venv/bin/python scripts/alta_tmve8_pr17.py --apply --force`
seguido de `bash deploy.sh` (reinicia el servicio y recarga el catálogo).

Idempotente: TMVE8 va por `instruments_abm.save_instrument` (el borde del ABM, con sus guards:
tipo analítico → fila ancla, `tc_inicial` obligatorio); PR17 se escribe por ORM como TB27
(no hay hoja «Provinciales» en el ABM) y se saltea si ya existe.

PROCEDENCIA
 · TMVE8 — «Bono del Tesoro Nacional en moneda dual TAMAR / dólar linked» (ficha BYMA: ISIN
   AR0821229090, emisión 2026-07-31, vto 2028-01-31, moneda Dólares). TC inicial 1499,8387 =
   A3500 del 28/07/2026 (día de la licitación): reproduce al centavo el pago del riel TAMAR que
   publica eldashboard (214.842,38 con TEM devengada 2,0166 % y 18 meses 30/360).
 · PR17 — «Bonos de Consolidación Décima Serie» (ficha BYMA: ISIN ARARGE320CT6, emisión
   2022-05-02, vto 2029-05-02). Amortiza 10 cuotas del 7 %, 2 del 9 % y 1 del 12 % del monto
   adeudado, trimestrales desde 2026-05-02; interés BADLAR bancos privados sobre saldo,
   capitalizado trimestralmente hasta 2026-02-02 y pagadero desde 2026-05-02. Monto adeudado a
   2026-02-02 = 820,592814 por 100 VN: el residual 705,70982 de eldashboard (2026-09-08) tras
   dos cuotas del 7 %. Cupones proyectados PLANOS a la BADLAR del cupón corriente (22,8349 %,
   eldashboard), ACT/365 sobre días reales. Es la foto que usa el catálogo para TB27/BAS26/SFN27
   (VanillaStrategy sobre flujos explícitos): cuando la BADLAR se mueva, regenerar el schedule.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_SCRIPTS = ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from op_guards import guard_write  # noqa: E402

TMVE8_FIELDS: Dict[str, Any] = {
    "ticker_ars": "TMVE8",
    "tipo": "DUAL_DL_TAMAR",
    "short_name": "Bono Dual TAMAR / Dolar Linked 31-01-2028",
    "isin": "AR0821229090",
    "fecha_emision": "2026-07-31",
    "fecha_vencimiento": "2028-01-31",
    "base calculo": "30/360",
    "spread": "0",
    "tc_inicial": "1499.8387",          # A3500 del 28/07/2026 (licitación)
}

PR17_TICKER = "PR17"
PR17_CAPITAL_AJUSTADO = 705.70982004 / (1.0 - 2 * 0.07)   # monto adeudado a 2026-02-02, por 100 VN
PR17_BADLAR = 0.22834918478260868                           # cupón corriente (2026-08-02 → 2026-11-02)
PR17_INICIO_PAGOS = date(2026, 2, 2)                        # fin de la capitalización
_PR17_PCT = [0.07] * 10 + [0.09] * 2 + [0.12]


def pr17_cashflows() -> List[Tuple[date, float, float]]:
    """13 filas (fecha, amortización, interés) por 100 VN original, desde 2026-05-02."""
    fechas: List[date] = []
    y, m = 2026, 5
    for _ in range(13):
        fechas.append(date(y, m, 2))
        m += 3
        if m > 12:
            m, y = m - 12, y + 1
    out: List[Tuple[date, float, float]] = []
    saldo, prev = PR17_CAPITAL_AJUSTADO, PR17_INICIO_PAGOS
    for f, pct in zip(fechas, _PR17_PCT):
        amort = round(PR17_CAPITAL_AJUSTADO * pct, 6)
        interes = round(saldo * PR17_BADLAR * (f - prev).days / 365.0, 6)
        out.append((f, amort, interes))
        saldo -= amort
        prev = f
    return out


def _pr17_orm():
    from core.infrastructure.db.models import CashflowORM, InstrumentORM

    orm = InstrumentORM(
        ticker=PR17_TICKER, short_name="BOCON Décima Serie BADLAR 2029",
        instrument_type="PROVINCIAL ARS", isin="ARARGE320CT6",
        maturity_date=date(2029, 5, 2), emission_date=date(2022, 5, 2),
        category="Provinciales", payment_frequency=4, day_count="ACT/365",
        sheet="Provinciales", cer_lag=10,
        raw_fields={
            "tipo": "PROVINCIAL ARS", "ley_aplicable": "ARG",
            "origen": "ficha BYMA + eldashboard 2026-09-08 (residual 705.70982, BADLAR cupón "
                      "corriente 22.8349%); schedule scripts/alta_tmve8_pr17.py",
            "capital_ajustado_2026_02_02": round(PR17_CAPITAL_AJUSTADO, 6),
            "badlar_cupon": PR17_BADLAR,
        },
    )
    orm.cashflows = [CashflowORM(ticker=PR17_TICKER, fecha_pago=f, amortizacion=a,
                                 cupon_interes=i, es_ancla=False)
                     for f, a, i in pr17_cashflows()]
    return orm


def apply_altas() -> Dict[str, str]:
    """Escribe los dos (sin guards ni backup: los pone `main`). Devuelve {ticker: acción}."""
    from apps.web.instruments_abm import save_instrument
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    init_db()
    out: Dict[str, str] = {}
    res = save_instrument("TAMAR", dict(TMVE8_FIELDS), cashflows=None)
    out["TMVE8"] = res["action"]
    with SessionLocal.begin() as s:
        if s.get(InstrumentORM, PR17_TICKER) is not None:
            out[PR17_TICKER] = "skipped (ya existe)"
        else:
            s.add(_pr17_orm())
            out[PR17_TICKER] = "created"
    return out


def verify(prices: Optional[Dict[str, float]] = None) -> Dict[str, Dict[str, Optional[float]]]:
    """TIR / V.Téc / MD con providers REALES (red) para chequear contra eldashboard."""
    from apps.web import bond_detail
    from core.domain.models import MarketSnapshot
    from core.domain.services import FinancialEngine
    from core.infrastructure.db.catalog_repository import CatalogRepository
    from core.infrastructure.fx_provider import DolarAPIProvider
    from core.infrastructure.indices_provider import BCRAIndicesProvider

    prices = prices or {"TMVE8": 139680.0, PR17_TICKER: 643.129219}
    repo = CatalogRepository(auto_seed=False)
    idx, fx = BCRAIndicesProvider(), DolarAPIProvider()
    settle = bond_detail._resolve_ref(1)
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for tk, px in prices.items():
        inst = repo.get_instrument_by_ticker(tk)
        if inst is None:
            out[tk] = {"error": None}
            continue
        snap = MarketSnapshot(instrument=inst, price=px)
        tir = FinancialEngine.calculate_tir(snap, idx, fx, settle_date=settle)
        vt = FinancialEngine.calculate_technical_value(snap, idx, fx, ref_date=settle, settle_lag=0)
        md = FinancialEngine.calculate_duration(snap, tir, settle_date=settle) if tir is not None else None
        out[tk] = {"precio": px, "tir": tir, "vtec": vt, "paridad": (px / vt) if vt else None, "md": md}
    return out


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    apply, force, do_verify = "--apply" in argv, "--force" in argv, "--verify" in argv

    from config.settings import settings
    print(f"catálogo: {settings.catalog_db}")
    print("TMVE8 (hoja TAMAR, DUAL_DL_TAMAR):", {k: v for k, v in TMVE8_FIELDS.items()})
    print(f"PR17 (PROVINCIAL ARS): capital ajustado {PR17_CAPITAL_AJUSTADO:.6f}, "
          f"BADLAR {PR17_BADLAR:.4%}, 13 cuotas:")
    for f, a, i in pr17_cashflows():
        print(f"   {f}  amort {a:10.6f}  interés {i:9.6f}")

    if do_verify:
        for tk, m in verify().items():
            print(f"verify {tk}: {m}")
        return 0
    if not apply:
        print("\n== DRY RUN (no escribe). Para aplicar: --apply ==")
        return 0
    rc = guard_write("pre-alta-tmve8-pr17", force=force)
    if rc:
        return rc
    print("OK:", apply_altas())
    print("Reiniciá el server para verlos en los paneles (en prod: bash deploy.sh).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
