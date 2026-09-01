"""Completa el NOMBRE DEL EMISOR de las ON tomandolo del Universo BYMA.

POR QUE: el panel ON agrupa por SECTOR, y el sector se deduce por keywords sobre el
nombre del emisor (`core/domain/on_classification.py`). El alta masiva del informe
IAMC no traia razon social, asi que dejo `short_name == ticker` en 159 ON: sin
nombre no hay match posible y todas cayeron en "Otros".

DE DONDE SALE: la tabla `byma_catalog` (Universo BYMA, ~4.7k especies, columna
`emisor`), que ya vive en la misma DB. Se busca por el ticker base y, si no aparece,
por las patas MEP/CABLE — la ficha suele estar cargada en una sola de las tres.

Idempotente: solo toca filas cuyo `short_name` este vacio o sea igual al ticker.
Nunca pisa un nombre ya cargado a mano desde el ABM. Snapshot pre-op VERIFICADO
(`op_guards.guard_write`): si el server esta vivo o el backup fallo, no escribe.

    py -3.12 scripts/backfill_on_emisor.py --dry-run
    py -3.12 scripts/backfill_on_emisor.py
    py -3.12 scripts/backfill_on_emisor.py --force   # saltea los guards
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from core.domain.on_classification import classify_sector  # noqa: E402
from core.infrastructure.db.engine import SessionLocal  # noqa: E402
from core.infrastructure.db.models import BymaCatalogORM, InstrumentORM  # noqa: E402
from scripts.op_guards import guard_write  # noqa: E402

SHEET = "Obligaciones_Negociables"

# ON sin ficha en el Universo BYMA, identificadas a mano contra fuentes publicas
# (2026-08-31). Solo se cargan las que tienen fuente verificable; las que no se
# pudieron identificar (EAC4O, RAC8O, SIC2O) se dejan sin emisor a proposito:
# un nombre equivocado clasifica mal el bono y es peor que dejarlo en "Otros".
MANUAL: dict[str, str] = {
    "DNCBO": "Empresa Distribuidora y Comercializadora Norte S.A.",  # EDENOR
    "OZC8O": "Empresa Distribuidora de Electricidad de Mendoza S.A.",  # EDEMSA
    "MTC2O": "Mastellone Hermanos S.A.",                             # La Serenisima
}


def _needs_fix(short_name: str | None, ticker: str) -> bool:
    sn = (short_name or "").strip()
    return not sn or sn == ticker


def main(dry: bool, force: bool = False) -> int:
    # Preflight ANTES de leer nada: si no se puede escribir con red de seguridad,
    # mejor abortar sin haber recorrido el catalogo. El dry-run no escribe, asi
    # que corre igual con el monitor arriba (es su uso normal: previsualizar).
    if not dry and (rc := guard_write("pre-on-emisor", force=force)):
        return rc

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
                        if t and t in universo), None) or MANUAL.get(o.ticker)
            (resueltos if emi else sin_resolver).append((o, emi))

        print(f"\nresueltos: {len(resueltos)}  |  sin resolver: {len(sin_resolver)}")
        if sin_resolver:
            print("  sin ficha en el universo:", [o.ticker for o, _ in sin_resolver])

        if not dry:
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
    raise SystemExit(main("--dry-run" in sys.argv, force="--force" in sys.argv))
