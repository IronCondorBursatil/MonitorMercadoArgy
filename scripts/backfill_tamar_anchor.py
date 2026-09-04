"""Migración de datos: inserta la fila ANCLA de los instrumentos de payoff ANALÍTICO.

Los tipos de `instrument_groups.ANALYTIC_PAYOFF_TYPES` (TAMAR PURO / DUAL /
DUAL_CER_TAMAR — 14 bonos en la `catalog.db` viva) no tienen NINGUNA fila en
`cashflows`, y eso es correcto: su pago a vencimiento sale de `tamar.tamar_dual_payoff_at`
sobre la TAMAR observada+proyectada, así que materializarles un schedule nominal sería
un ERROR de datos, no una optimización.

El costo de no tener ninguna fila es que quedan indistinguibles de un bono a medio
cargar: invisibles en `/cashflows` y sin vencimiento auditable desde la tabla de flujos.
La fila **ancla** (`CashflowORM.es_ancla = 1`, `fecha_pago = vencimiento`, montos 0) los
hace visibles EN LA DB sin tocar el pricing: `catalog_repository._orm_to_domain` la
filtra, así que al motor le siguen llegando con `cashflows=()` — bit-idéntico por
construcción. Ese es el invariante que este script NO puede romper.

FORWARD-ONLY: sólo hace INSERT de la fila ancla sobre bonos analíticos que hoy no tienen
NINGUNA fila. Nunca borra, nunca actualiza montos, nunca toca un bono con flujos cargados
(si alguien le cargó un schedule a mano, limpiarlo es una decisión de datos que este
script no toma: lo reporta). Idempotente por CONTENIDO — el segundo paso no encuentra
candidatos, así que correrlo dos veces no cambia nada.

    py -3.12 scripts/backfill_tamar_anchor.py            # DRY RUN (default)
    py -3.12 scripts/backfill_tamar_anchor.py --apply    # escribe (backup pre-op)
    py -3.12 scripts/backfill_tamar_anchor.py --apply --force   # con el server vivo
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.domain.instrument_groups import has_closed_form_payoff  # noqa: E402
from op_guards import guard_write  # noqa: E402

# Identidad de la migración: se sella en `schema_meta` para auditoría (qué corrió y
# cuándo). NO reemplaza la idempotencia por contenido — el sello es informativo.
MIGRATION_ID = "2026-09-04-tamar-anchor-cashflows"


def _analiticos(rows) -> List[Any]:
    return [o for o in rows if has_closed_form_payoff(getattr(o, "instrument_type", ""))]


def build_plan(rows) -> List[Dict[str, Any]]:
    """Plan: [{ticker, tickers, tipo, vto}] — un bono analítico entra SÓLO si

      · no tiene NINGUNA fila de cashflow (ni ancla ni real), y
      · tiene `maturity_date` (sin vencimiento no hay ancla que inventar).

    Un bono que ya tiene su ancla, o que tiene flujos cargados a mano, queda afuera:
    de ahí la idempotencia y el carácter no-destructivo."""
    plan: List[Dict[str, Any]] = []
    for o in _analiticos(rows):
        if list(getattr(o, "cashflows", []) or []):
            continue
        vto = getattr(o, "maturity_date", None)
        if vto is None:
            continue
        tickers = [t for t in (getattr(o, "ticker", None), getattr(o, "ticker_mep", None),
                               getattr(o, "ticker_ccl", None)) if t]
        plan.append({"ticker": o.ticker, "tickers": tickers,
                     "tipo": o.instrument_type, "vto": vto})
    plan.sort(key=lambda e: e["ticker"])
    return plan


def sin_vencimiento(rows) -> List[str]:
    """Analíticos SIN filas y SIN `maturity_date`: el script no adivina la fecha.
    Hay que completarles el vencimiento por ABM y volver a correrlo."""
    return sorted(o.ticker for o in _analiticos(rows)
                  if not list(getattr(o, "cashflows", []) or [])
                  and getattr(o, "maturity_date", None) is None)


def con_flujos_cargados(rows) -> List[str]:
    """Analíticos que YA tienen filas NO-ancla. El script no las borra (podrían ser un
    dato deliberado del operador) pero avisa: mientras estén, llegan al dominio y le
    cambian el pricing a ese bono respecto de sus pares."""
    out = []
    for o in _analiticos(rows):
        cfs = list(getattr(o, "cashflows", []) or [])
        if cfs and any(not getattr(cf, "es_ancla", False) for cf in cfs):
            out.append(o.ticker)
    return sorted(out)


def _stamp(s) -> None:
    """Sella la migración en `schema_meta` (auditoría; no gatea la idempotencia)."""
    from sqlalchemy import text

    s.execute(
        text("INSERT INTO schema_meta (key, value) VALUES (:k, :v) "
             "ON CONFLICT(key) DO UPDATE SET value=excluded.value"),
        {"k": f"migration:{MIGRATION_ID}", "v": datetime.now().isoformat(timespec="seconds")},
    )


def apply_migration() -> int:
    """Aplica el plan en UNA transacción. Devuelve cuántos bonos se ancló.
    Sin backup ni guards (los pone `main`) — pensado para tests y para reuso."""
    from sqlalchemy import select

    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import CashflowORM, InstrumentORM

    init_db()
    with SessionLocal.begin() as s:
        rows = s.execute(select(InstrumentORM)).scalars().all()
        plan = build_plan(rows)
        by_ticker = {o.ticker: o for o in rows}
        for e in plan:
            o = by_ticker[e["ticker"]]
            # append, NO reasignación de la colección: `orm.cashflows = [...]` haría un
            # delete-orphan de lo que hubiera. Acá no hay nada (es la condición del plan),
            # pero el append deja el script incapaz de borrar por accidente.
            o.cashflows.append(CashflowORM(ticker=o.ticker, fecha_pago=e["vto"],
                                           amortizacion=0.0, cupon_interes=0.0,
                                           es_ancla=True))
        if plan:
            _stamp(s)
    return len(plan)


def print_plan(plan, faltan_vto, con_flujos) -> None:
    # Sin glifos fuera de cp1252 en lo que se IMPRIME: la consola de Windows los
    # rompe (misma convención que scripts/migrate_orphan_types.py::_fmt).
    if not plan:
        print("Nada que anclar: ningun bono de payoff analitico sin filas.")
    for e in plan:
        print(f"  {e['ticker']:<7} [{' / '.join(e['tickers'])}]  {e['tipo']:<15} "
              f"ancla -> {e['vto']}")
    if faltan_vto:
        print(f"\nSIN VENCIMIENTO ({len(faltan_vto)}): no se anclan; completalos por ABM.")
        print("  " + " ".join(faltan_vto))
    if con_flujos:
        print(f"\nCON FLUJOS CARGADOS ({len(con_flujos)}): NO se tocan (forward-only), pero "
              "esos flujos SI llegan al dominio y les cambian el pricing.")
        print("  " + " ".join(con_flujos))


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    apply = "--apply" in argv
    force = "--force" in argv

    from sqlalchemy import select

    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    init_db()
    with SessionLocal() as s:
        rows = s.execute(select(InstrumentORM)).scalars().all()
        plan = build_plan(rows)
        faltan_vto = sin_vencimiento(rows)
        con_flujos = con_flujos_cargados(rows)
        especies = sum(len(e["tickers"]) for e in plan)

    print(f"migracion {MIGRATION_ID}")
    print(f"bonos de payoff analitico sin filas y con vencimiento: {len(plan)} "
          f"({especies} especies con sus patas de moneda)\n")
    print_plan(plan, faltan_vto, con_flujos)

    if not apply:
        print("\n== DRY RUN (no escribe). Para aplicar: --apply ==")
        return 0
    if not plan:
        return 0

    rc = guard_write("pre-tamar-anchor", force=force)
    if rc:
        return rc
    n = apply_migration()
    print(f"\nOK: {n} bono(s) anclados. El pricing NO cambia (el ancla no entra al "
          "dominio); ahora aparecen en /cashflows con su vencimiento.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
