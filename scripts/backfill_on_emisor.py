"""Completa el NOMBRE DEL EMISOR de las ON tomandolo del Universo BYMA.

POR QUE: el panel ON agrupa por SECTOR, y el sector se deduce por keywords sobre el
nombre del emisor (`core/domain/on_classification.py`). El alta masiva del informe
IAMC no traia razon social, asi que dejo `short_name == ticker` en 159 ON: sin
nombre no hay match posible y todas cayeron en "Otros".

DE DONDE SALE: la tabla `byma_catalog` (Universo BYMA, ~4.7k especies, columna
`emisor`), que ya vive en la misma DB. Se busca por el ticker base y, si no aparece,
por las patas MEP/CABLE — la ficha suele estar cargada en una sola de las tres.

Idempotente: solo toca filas cuyo `short_name` este vacio o sea igual al ticker.
Nunca pisa un nombre ya cargado a mano desde el ABM. Snapshot pre-op.

    py -3.12 scripts/backfill_on_emisor.py --dry-run
    py -3.12 scripts/backfill_on_emisor.py
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from config.settings import settings  # noqa: E402
from core.domain.on_classification import classify_sector  # noqa: E402
from core.infrastructure.db.backup import backup_db  # noqa: E402
from core.infrastructure.db.engine import SessionLocal  # noqa: E402
from core.infrastructure.db.models import BymaCatalogORM, InstrumentORM  # noqa: E402

SHEET = "Obligaciones_Negociables"


def _needs_fix(short_name: str | None, ticker: str) -> bool:
    sn = (short_name or "").strip()
    return not sn or sn == ticker


def main(dry: bool) -> int:
    with SessionLocal() as s:
        ons = s.scalars(select(InstrumentORM).where(InstrumentORM.sheet == SHEET)).all()

        # Indice symbol -> emisor del universo BYMA (una pasada, no N queries).
        universo = {
            sym: (emi or "").strip()
            for sym, emi in s.execute(
                select(BymaCatalogORM.symbol, BymaCatalogORM.emisor)).all()
            if (emi or "").strip()
        }

        pendientes = [o for o in ons if _needs_fix(o.short_name, o.ticker)]
        print(f"ON en la hoja: {len(ons)}  |  sin nombre de emisor: {len(pendientes)}")
        print(f"universo BYMA con emisor: {len(universo)}")

        resueltos, sin_resolver = [], []
        for o in pendientes:
            emi = next((universo[t] for t in (o.ticker, o.ticker_mep, o.ticker_ccl)
                        if t and t in universo), None)
            (resueltos if emi else sin_resolver).append((o, emi))

        print(f"\nresueltos: {len(resueltos)}  |  sin resolver: {len(sin_resolver)}")
        if sin_resolver:
            print("  sin ficha en el universo:", [o.ticker for o, _ in sin_resolver])

        if not dry:
            backup_db(settings.catalog_db, settings.backup_dir,
                      keep=settings.backup_keep, tag="pre-on-emisor")
            for o, emi in resueltos:
                o.short_name = emi
            s.commit()
            print(f"\nescritos: {len(resueltos)}")
        else:
            print("\n== DRY RUN (no escribe) ==")

        # Como queda la clasificacion por sector con los nombres ya resueltos.
        final = {}
        for o in ons:
            sn = (o.short_name or "").strip()
            if _needs_fix(sn, o.ticker):
                sn = next((e for o2, e in resueltos if o2.ticker == o.ticker), "") or ""
            if sn and sn != o.ticker:
                final[o.ticker] = sn

        print("\n--- distribucion por sector ---")
        cnt = collections.Counter(classify_sector(e) for e in final.values())
        for k, v in cnt.most_common():
            print(f"   {k:<36} {v}")

        otros = sorted({e for e in final.values() if classify_sector(e) == "Otros"})
        print(f"\n--- emisores que quedan en Otros ({len(otros)} distintos) ---")
        for e in otros:
            print("   ", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--dry-run" in sys.argv))
