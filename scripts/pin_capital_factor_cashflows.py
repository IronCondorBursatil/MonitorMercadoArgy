"""Pin de cashflows EXPLÍCITOS para los bonos con capital factor != 1.

Contexto: 7 bonos tienen capital factor != 1 — 3 CER reestructurados (CUAP/DICP/DIP0,
factor > 1, cashflows escalados > 100) y 4 ONs amortizadas (ARC1O/CAC5O/IRCFO/PNDCO,
factor < 1, face residual). El `capital factor` es un INPUT del sintetizador
(cashflow_synth) que escala el schedule; el MOTOR no lo lee (deriva la normalización de
Σamort). Su schedule ya está materializado en catalog.db.

Este script RE-GUARDA ese schedule como cashflow explícito (idéntico) y saca `capital
factor` de raw_fields, para decouplar estos 7 bonos del sintetizador (paso previo a
eliminar el campo). Backup pre-op + restore automático si CUALQUIER métrica
(TIR/V.Téc/MD) cambia. Es seguro porque los cashflows quedan byte-idénticos.

    py -3.12 scripts/pin_capital_factor_cashflows.py          # aplica (con backup+verify)
    py -3.12 scripts/pin_capital_factor_cashflows.py --dry-run  # solo muestra, no escribe
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root → import apps/core

from apps.web.instruments_abm import get_instrument, list_instruments, save_instrument  # noqa: E402
from config.settings import settings
from core.domain.models import MarketSnapshot
from core.domain.services import FinancialEngine as FE
from core.infrastructure.db.backup import restore_db
from core.infrastructure.db.catalog_repository import CatalogRepository
from scripts.op_guards import guard_write_snapshot

SETTLE = date(2026, 6, 10)


class _CER:
    def get_cer(self, target=None):
        return 500.0

    def get_tamar(self, target=None):
        return None


class _FX:
    def get_mayorista_venta(self):
        return 1000.0
    get_mep_venta = get_ccl_venta = get_mayorista_venta


def _cfs_tuple(cfs):
    return tuple(sorted((str(c["date"])[:10], round(float(c["amortization"]), 9),
                         round(float(c["interest"]), 9)) for c in cfs))


def _metrics(ticker, sheet):
    """(tir, vtec, md) del bono cargado en la DB, con índices/FX stub determinista. El
    precio se deriva de la V.Téc (~55% paridad) para que la TIR sea sensata. La verificación
    es de INVARIANCIA (antes==después): mismo input → mismo output si los cashflows no cambian."""
    inst = CatalogRepository(auto_seed=False).get_instrument_by_ticker(ticker)
    if inst is None:
        return None
    vt = FE.calculate_technical_value(MarketSnapshot(instrument=inst, price=100.0),
                                      _CER(), _FX(), ref_date=SETTLE)
    price = max(1.0, round(0.55 * (vt or 100.0), 2))
    snap = MarketSnapshot(instrument=inst, price=price)
    tir = FE.calculate_tir(snap, _CER(), _FX(), settle_date=SETTLE)
    md = FE.calculate_duration(snap, tir, settle_date=SETTLE)
    return tir, vt, md


def _delta(a, b):
    if a is None or b is None:
        return 0.0 if a == b else float("inf")
    return abs(a - b)


def main(dry: bool, force: bool = False) -> int:
    targets = []
    for it in list_instruments():
        g = get_instrument(it["key"]) or {}
        try:
            capf = float((g.get("fields") or {}).get("capital factor"))
        except (TypeError, ValueError):
            continue
        if abs(capf - 1.0) > 1e-6:
            targets.append((it["key"], g["sheet"], capf, g))

    print(f"Bonos con capital factor != 1: {len(targets)}")
    for tk, sheet, capf, _ in sorted(targets, key=lambda x: (x[1], x[0])):
        print(f"  {tk:8} {sheet:26} capital_factor={capf}")
    if dry or not targets:
        print("\n(dry-run: no se escribe nada)" if dry else "\nNada para hacer.")
        return 0

    # El snapshot NO puede ser opcional: si falla, `bkp` queda en None y el rollback
    # de más abajo explota (`Path(None)` → TypeError) con el catálogo ya mutado a
    # medias. `guard_write_snapshot` verifica el retorno (y el server vivo).
    rc, bkp = guard_write_snapshot("pre-explicit-capf", force=force)
    if rc:
        return rc
    print()

    def _tickers(fields):
        return {str(fields.get(k)).upper() for k in ("ticker", "ticker_ars", "ticker_mep",
                "ticker_ccl") if fields.get(k)}

    failures = []
    for tk, sheet, capf, g in targets:
        before_cfs = _cfs_tuple(g["cashflows"])
        before_m = _metrics(tk, sheet)
        before_tks = _tickers(g["fields"])
        fields = {k: v for k, v in g["fields"].items() if k != "capital factor"}
        save_instrument(sheet, fields, cashflows=g["cashflows"])   # explícito, sin capital factor
        after = get_instrument(tk) or {}
        after_cfs = _cfs_tuple(after.get("cashflows", []))
        after_m = _metrics(tk, sheet)
        cf_ok = before_cfs == after_cfs
        capf_gone = "capital factor" not in (after.get("fields") or {})
        tk_ok = before_tks == _tickers(after.get("fields") or {})
        dt = max(_delta(before_m[i], after_m[i]) for i in range(3)) if (before_m and after_m) else float("inf")
        ok = cf_ok and capf_gone and tk_ok and dt < 1e-6
        print(f"  [{'OK ' if ok else 'FAIL'}] {tk:8} cf_idénticos={cf_ok} patas_ok={tk_ok} "
              f"capital_factor_fuera={capf_gone} ΔmaxMétrica={dt:.2e}")
        if not ok:
            failures.append(tk)

    if failures:
        if bkp is None:   # sólo con --force: el operador aceptó correr sin red
            print(f"\n⚠ Cambios detectados en {failures} y NO hay backup (--force): "
                  "restaurá a mano con scripts/restore_catalog.py.")
            return 1
        print(f"\n⚠ Cambios detectados en {failures} → RESTAURANDO backup (sin cambios).")
        restore_db(bkp, settings.catalog_db)
        return 1
    print(f"\n✓ {len(targets)} bonos re-guardados con cashflow EXPLÍCITO, capital factor "
          "sacado de raw_fields, TIR/V.Téc/MD sin cambios.")
    return 0


if __name__ == "__main__":
    # `--dry` se mantiene como alias del viejo nombre; `--dry-run` es el del resto
    # de los scripts, y antes caía en la rama que ESCRIBE.
    raise SystemExit(main(dry=bool({"--dry", "--dry-run"} & set(sys.argv)),
                          force="--force" in sys.argv))
