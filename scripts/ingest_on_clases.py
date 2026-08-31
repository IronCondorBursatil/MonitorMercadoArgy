"""Carga la CLASE de las ON y AUDITA el vencimiento contra el catalogo.

La clase ("Clase XXXIX", "Serie IV Clase A") es display-only y vive en
`raw_fields.serie_clase`. El panel ON la muestra en su columna; sin ella la fila
queda con un guion y no se distingue una serie de otra del mismo emisor.

NO corrige el cronograma: si el vencimiento declarado difiere del que tiene el
catalogo, lo REPORTA. Un vto distinto cambia la TIR, asi que la correccion se
decide a mano y no se aplica en silencio.

Match por ticker base o por la pata MEP. Idempotente. Snapshot pre-op.

    py -3.12 scripts/ingest_on_clases.py --dry-run
    py -3.12 scripts/ingest_on_clases.py [ruta.csv]
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from config.settings import settings  # noqa: E402
from core.infrastructure.db.backup import backup_db  # noqa: E402
from core.infrastructure.db.engine import SessionLocal  # noqa: E402
from core.infrastructure.db.models import InstrumentORM  # noqa: E402

DEFAULT = ROOT / "data" / "iamc" / "on_clases_2026_08.csv"


def _fecha(txt: str):
    """dd/mm/aaaa -> date. None si viene incompleta (ej. '27/02/20**')."""
    try:
        return datetime.strptime((txt or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def main(dry: bool, ruta: Path) -> int:
    filas = list(csv.DictReader(ruta.open(encoding="utf-8")))
    print(f"fuente: {ruta.name} ({len(filas)} filas)\n")

    with SessionLocal() as s:
        escritos, faltantes, malformadas, avisos = 0, [], [], []
        for f in filas:
            tk = (f.get("ticker") or "").strip()
            clase = (f.get("clase") or "").strip()
            if not tk or not clase or clase == tk or clase.rstrip("DC") == tk.rstrip("O"):
                # La clase no puede ser el propio ticker: fila mal armada en la fuente.
                malformadas.append(tk or "?")
                continue

            mep = (f.get("ticker_mep") or "").strip()
            o = s.scalars(select(InstrumentORM).where(
                (InstrumentORM.ticker == tk)
                | ((InstrumentORM.ticker_mep == mep) if mep else False))).first()
            if o is None:
                faltantes.append(tk)
                continue

            vto = _fecha(f.get("vencimiento", ""))
            if vto is None and (f.get("vencimiento") or "").strip():
                malformadas.append(f"{tk} (vto '{f['vencimiento']}')")
            elif vto and o.maturity_date and o.maturity_date != vto:
                avisos.append(f"  {o.ticker:<6} catalogo {o.maturity_date}  !=  "
                              f"declarado {vto}")

            rf = dict(o.raw_fields or {})
            if rf.get("serie_clase") != clase:
                rf["serie_clase"] = clase
                if not dry:
                    o.raw_fields = rf
                    flag_modified(o, "raw_fields")
                escritos += 1

        print(f"clases a escribir : {escritos}")
        if faltantes:
            print(f"no estan en el catalogo ({len(faltantes)}): {faltantes}")
        if malformadas:
            print(f"filas descartadas ({len(malformadas)}): {malformadas}")

        print(f"\n--- auditoria de vencimiento ({len(avisos)} diferencias) ---")
        for a in avisos or ["  (ninguna: coinciden con el catalogo)"]:
            print(a)

        if dry:
            print("\n== DRY RUN (no escribe) ==")
        else:
            backup_db(settings.catalog_db, settings.backup_dir,
                      keep=settings.backup_keep, tag="pre-on-clases")
            s.commit()
            print(f"\nescritos: {escritos}")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    raise SystemExit(main("--dry-run" in sys.argv, Path(args[0]) if args else DEFAULT))
