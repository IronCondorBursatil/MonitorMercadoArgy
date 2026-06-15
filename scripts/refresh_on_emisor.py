"""Refresca el EMISOR de todas las Obligaciones Negociables al valor que trae BYMADATA
(`byma_catalog.emisor`), matcheando por ISIN (fallback: ticker_pesos). Así todas las ON
del mismo emisor quedan con el MISMO valor (ej. "YPF S.A.", "BANCO DE GALICIA Y BUENOS
AIRES S.A.") y la CLASE queda SOLO en `serie_clase` (se saca del emisor).

- Backup del catálogo ANTES de tocar nada (backup_db).
- Idempotente / re-ejecutable (corré de nuevo tras un re-seed del master).
- Para las ON sin match en BYMA, fallback: emisor limpio de raw_fields["short_name"]
  (o se le quita el sufijo " - Clase/Serie …" al short_name actual).

Uso:
    py -3.12 scripts/refresh_on_emisor.py --dry   # muestra qué cambiaría, sin escribir
    py -3.12 scripts/refresh_on_emisor.py         # aplica (con backup previo)
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from config.settings import settings  # noqa: E402
from core.infrastructure.db.backup import backup_db  # noqa: E402
from core.infrastructure.db.catalog_repository import init_db  # noqa: E402
from core.infrastructure.db.engine import SessionLocal  # noqa: E402
from core.infrastructure.db.models import BymaCatalogORM, InstrumentORM  # noqa: E402

ON_SHEET = "Obligaciones_Negociables"
# sufijo de clase/serie pegado al emisor: " - Clase XXX" / " – Serie 13 Clase A"
_CLASS_RE = re.compile(r"\s*[-–]\s*(clase|serie)\b.*$", re.IGNORECASE)


def _strip_class(name: str) -> str:
    return _CLASS_RE.sub("", name or "").strip()


def main(dry: bool) -> int:
    init_db()
    if not dry:
        path = backup_db(settings.catalog_db, settings.backup_dir,
                         keep=settings.backup_keep, tag="pre_on_emisor_refresh")
        print(f"backup: {path}")

    with SessionLocal() as s:
        # Mapa BYMADATA: isin -> emisor y ticker_pesos -> emisor (todas las filas de un
        # ISIN comparten el mismo emisor; nos quedamos con el primero no vacío).
        by_isin: dict[str, str] = {}
        by_tk: dict[str, str] = {}
        for isin, tkp, em in s.execute(
                select(BymaCatalogORM.isin, BymaCatalogORM.ticker_pesos, BymaCatalogORM.emisor)).all():
            if not em:
                continue
            if isin:
                by_isin.setdefault(isin, em)
            if tkp:
                by_tk.setdefault(tkp.upper(), em)

        ons = s.execute(select(InstrumentORM).where(InstrumentORM.sheet == ON_SHEET)).scalars().all()
        changes = []      # (ticker, old, new, src)
        n_byma = n_fallback = n_unchanged = 0
        for o in ons:
            byma_em = (o.isin and by_isin.get(o.isin)) or by_tk.get((o.ticker or "").upper())
            if byma_em:
                new, src = byma_em.strip(), "byma"
            else:
                rf0 = o.raw_fields or {}
                new = _strip_class((rf0.get("short_name") or "").strip() or _strip_class(o.short_name or ""))
                src = "fallback"
            if not new or new == (o.short_name or ""):
                n_unchanged += 1
                continue
            changes.append((o.ticker, o.short_name, new, src))
            n_byma += src == "byma"
            n_fallback += src == "fallback"
            if not dry:
                o.short_name = new
                rf = dict(o.raw_fields or {})
                rf["short_name"] = new
                o.raw_fields = rf   # reasignar el dict → JSON marcado dirty
        if not dry:
            s.commit()

    print(f"\nONs: {len(ons)} · a cambiar: {len(changes)} "
          f"(byma {n_byma} / fallback {n_fallback}) · sin cambio: {n_unchanged}")
    print(("DRY-RUN (no se escribió). " if dry else "APLICADO. ") + "Ejemplos:")
    for tk, old, new, src in changes[:25]:
        print(f"  {tk:8s} [{src:8s}] {old!r:42s} -> {new!r}")
    if len(changes) > 25:
        print(f"  … (+{len(changes) - 25} más)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(dry="--dry" in sys.argv))
