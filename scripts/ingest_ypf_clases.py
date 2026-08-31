"""Carga la CLASE de las ON de YPF y AUDITA vencimiento y cupon contra lo declarado.

La clase ("Clase XXXIX", "Senior Notes 2031") es display-only y vive en
`raw_fields.serie_clase`. El panel ON la muestra en la columna Clase; sin ella la
fila queda con un guion y no se distingue una serie de otra.

Ademas de escribir, COMPARA lo que ya tiene el catalogo contra el vto y el cupon
que declara la fuente, y reporta las diferencias. No corrige el cronograma en
silencio: un vto o un cupon distinto cambia la TIR, asi que se informa para
revisar a mano.

Los tickers de la fuente son las patas MEP (…D); la fila del catalogo es la base
(…O), asi que el match es por `ticker_mep`.

    py -3.12 scripts/ingest_ypf_clases.py --dry-run
    py -3.12 scripts/ingest_ypf_clases.py
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from config.settings import settings  # noqa: E402
from core.infrastructure.db.backup import backup_db  # noqa: E402
from core.infrastructure.db.engine import SessionLocal  # noqa: E402
from core.infrastructure.db.models import InstrumentORM  # noqa: E402

FUENTE = ROOT / "data" / "iamc" / "ypf_clases.csv"
_MES = {"ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AGO": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12}


def parse_vto_cupon(txt: str) -> tuple[tuple[int, int] | None, list[float]]:
    """'Sep-2033 (1,50% / 7,00%)' -> ((2033, 9), [1.5, 7.0]). Cupon puede faltar."""
    ym = None
    m = re.match(r"\s*([A-Za-z]{3})-(\d{4})", txt or "")
    if m and m.group(1).upper() in _MES:
        ym = (int(m.group(2)), _MES[m.group(1).upper()])
    cupones = [float(x.replace(",", ".")) for x in re.findall(r"(\d+[,.]\d+)\s*%", txt or "")]
    return ym, cupones


def main(dry: bool) -> int:
    filas = list(csv.DictReader(FUENTE.open(encoding="utf-8")))
    print(f"fuente: {len(filas)} clases\n")

    with SessionLocal() as s:
        escritos, faltantes, avisos = 0, [], []
        for f in filas:
            tk = f["ticker"].strip()
            o = s.scalars(select(InstrumentORM).where(
                (InstrumentORM.ticker_mep == tk) | (InstrumentORM.ticker == tk))).first()
            if o is None:
                faltantes.append(tk)
                continue

            ym, cupones = parse_vto_cupon(f["vto_cupon"])
            if ym and o.maturity_date:
                if (o.maturity_date.year, o.maturity_date.month) != ym:
                    avisos.append(f"  {o.ticker}: vto catalogo {o.maturity_date} "
                                  f"!= declarado {ym[1]:02d}/{ym[0]}")
            if cupones:
                actual = o.spread_rate
                # Step-up: la fuente declara dos tasas; comparamos contra la primera.
                if actual is not None and abs(float(actual) - cupones[0]) > 0.011:
                    avisos.append(f"  {o.ticker}: cupon catalogo {actual} "
                                  f"!= declarado {cupones[0]}"
                                  + (f" (step-up a {cupones[1]})" if len(cupones) > 1 else ""))

            rf = dict(o.raw_fields or {})
            if rf.get("serie_clase") != f["clase"].strip():
                rf["serie_clase"] = f["clase"].strip()
                if not dry:
                    o.raw_fields = rf
                    flag_modified(o, "raw_fields")
                escritos += 1

        print(f"clases a escribir: {escritos}")
        if faltantes:
            print(f"no encontrados en el catalogo: {faltantes}")
        print(f"\n--- auditoria vto/cupon ({len(avisos)} diferencias) ---")
        for a in avisos or ["  (ninguna: coinciden con el catalogo)"]:
            print(a)

        if dry:
            print("\n== DRY RUN (no escribe) ==")
        else:
            backup_db(settings.catalog_db, settings.backup_dir,
                      keep=settings.backup_keep, tag="pre-ypf-clases")
            s.commit()
            print(f"\nescritos: {escritos}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--dry-run" in sys.argv))
