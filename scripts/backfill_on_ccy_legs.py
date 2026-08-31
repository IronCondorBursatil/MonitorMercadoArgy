"""Completa las patas de moneda (MEP/CABLE) de las ON cargadas sin ellas.

POR QUÉ: una ON cotiza en 3 monedas — pesos (…O), MEP (…D) y cable (…C). El motor
emite UNA métrica POR PATA, y el panel ON las filtra por moneda (default MEP). Una ON
cargada solo con el ticker en pesos queda sin pata MEP/cable: no aparece bajo el filtro
por defecto y aporta 1 fila en vez de 3. Las ON del alta masiva del informe IAMC
entraron así (solo el ticker base) → el panel seguía mostrando ~129 filas.

QUÉ HACE: para cada ON sin patas, deriva los candidatos por sufijo (PLC4O → PLC4D /
PLC4C) y **verifica contra el feed vivo de BYMA** que esa pata realmente cotice antes
de escribirla. No inventa tickers: si la pata no está en el mercado, no se agrega.

Idempotente (solo toca filas sin pata). Snapshot pre-op. No destructivo.

    py -3.12 scripts/backfill_on_ccy_legs.py --dry-run
    py -3.12 scripts/backfill_on_ccy_legs.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SHEET = "Obligaciones_Negociables"


async def _live_symbols() -> set:
    """Símbolos con cotización en la fuente activa (BYMA open)."""
    from core.infrastructure.async_http import ResilientClient
    from core.infrastructure.byma.sources import make_source
    from core.infrastructure.provider_hub import ProviderHub
    from config.settings import settings

    client = ResilientClient()
    try:
        src = make_source(settings.market_source)
    except Exception:                                  # noqa: BLE001
        src = make_source("byma_open")
    hub = ProviderHub(client, active_source=src)
    await hub.refresh_all()
    snap = hub.snapshot()
    await client.aclose()
    # solo las que tienen precio > 0: una pata listada pero sin precio no sirve
    return {s for s, r in snap.items() if getattr(r, "c", None)}


def main(dry_run: bool = False) -> int:
    from sqlalchemy import select

    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    print("consultando el feed vivo...")
    live = asyncio.run(_live_symbols())
    print("símbolos con precio en el feed: %d\n" % len(live))

    with SessionLocal() as s:
        rows = s.execute(
            select(InstrumentORM.ticker, InstrumentORM.ticker_mep, InstrumentORM.ticker_ccl)
            .where(InstrumentORM.sheet == SHEET)).all()

    sin_patas = [t for t, m, c in rows if not m and not c]
    print("ON en la hoja: %d  |  sin patas de moneda: %d" % (len(rows), len(sin_patas)))

    plan, sin_match = {}, []
    for t in sin_patas:
        if not t.endswith("O"):        # la convención es …O (pesos) → …D / …C
            sin_match.append((t, "no termina en O"))
            continue
        base = t[:-1]
        mep, ccl = base + "D", base + "C"
        got_mep = mep if mep in live else None
        got_ccl = ccl if ccl in live else None
        if got_mep or got_ccl:
            plan[t] = (got_mep, got_ccl)
        else:
            sin_match.append((t, "ninguna pata cotiza"))

    print("con pata encontrada en el mercado: %d" % len(plan))
    print("sin pata (se dejan como están):   %d" % len(sin_match))
    for t, (m, c) in list(plan.items())[:10]:
        print("   %-8s -> mep=%-8s ccl=%s" % (t, m or "-", c or "-"))
    if len(plan) > 10:
        print("   ... y %d más" % (len(plan) - 10))

    if dry_run:
        print("\n== DRY RUN (no escribe) ==")
        return 0
    if not plan:
        print("\nNada para completar.")
        return 0

    from config.settings import settings
    from core.infrastructure.db.backup import backup_db

    snap = backup_db(settings.catalog_db, settings.backup_dir,
                     keep=settings.backup_keep, tag="pre-on-legs")
    print("\nbackup pre-op: %s" % snap)

    with SessionLocal.begin() as s:
        for t, (m, c) in plan.items():
            orm = s.get(InstrumentORM, t)
            if orm is None:
                continue
            if m:
                orm.ticker_mep = m
            if c:
                orm.ticker_ccl = c

    with SessionLocal() as s:
        rows = s.execute(
            select(InstrumentORM.ticker, InstrumentORM.ticker_mep, InstrumentORM.ticker_ccl)
            .where(InstrumentORM.sheet == SHEET)).all()
    con = sum(1 for _, m, c in rows if m or c)
    print("\nON con patas de moneda: %d/%d" % (con, len(rows)))
    print("Reiniciá el server para que el motor las precie en las 3 monedas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv))
