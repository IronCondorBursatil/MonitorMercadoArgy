"""Unifica los nombres de emisor de las ON que son la MISMA empresa escrita distinto.

EL PROBLEMA: el catalogo traia el emisor en formatos mezclados —
`YPF`, `YPF - Clase XXXIX` y `YPF S.A.` son tres emisores distintos para el panel,
que agrupa por nombre. El sufijo "- Clase XX" es informacion de SERIE que se colo
en el campo del emisor.

COMO UNIFICA (conservador a proposito): dos nombres se fusionan solo si su RAIZ
normalizada es identica — raiz = nombre sin el sufijo "- Clase ...", sin acentos,
en mayuscula y sin la forma societaria final (S.A./S.A.U./S.R.L./SACIF). El
canonico del grupo es la variante mas larga que queda, que es la mas completa
(`YPF S.A.` gana sobre `YPF`).

POR QUE ASI Y NO POR SIMILITUD: `YPF S.A.` (petrolera, tickers YM*) e
`YPF Energia Electrica S.A.` / `YPF LUZ` (generadora, tickers YF*) son
EMISORES DISTINTOS con creditos distintos. Un matcheo difuso las fusionaria y eso
seria un error financiero, no cosmetico. Con raiz exacta quedan separadas solas.
ALIAS cubre el unico caso que la raiz no puede resolver (`YPF LUZ` es la marca
comercial de `YPF Energia Electrica S.A.`).

Idempotente. Snapshot pre-op.

    py -3.12 scripts/normalize_on_emisor.py --dry-run
    py -3.12 scripts/normalize_on_emisor.py
"""
from __future__ import annotations

import collections
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from config.settings import settings  # noqa: E402
from core.infrastructure.db.backup import backup_db  # noqa: E402
from core.infrastructure.db.engine import SessionLocal  # noqa: E402
from core.infrastructure.db.models import InstrumentORM  # noqa: E402

SHEET = "Obligaciones_Negociables"

# Marca comercial -> razon social. Solo para lo que la raiz NO puede unificar sola.
ALIAS: dict[str, str] = {
    "YPF LUZ": "YPF Energía Eléctrica S.A.",
}

_CLASE_RE = re.compile(r"\s*[-–]\s*Clase\b.*$", re.IGNORECASE)
_SOC_RE = re.compile(r"\s+(S\.?\s?A\.?\s?U?\.?|S\.?R\.?L\.?|SACIFIA|SACIF|SAICF)$",
                     re.IGNORECASE)


def raiz(nombre: str) -> str:
    """Clave de agrupamiento: sin serie, sin acentos, sin forma societaria."""
    s = _CLASE_RE.sub("", nombre or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = s.replace(",", "").replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return _SOC_RE.sub("", s).strip()


def canonico(variantes: set[str]) -> str:
    """La variante mas completa del grupo: sin '- Clase', y a igualdad la mas larga
    (asi `YPF S.A.` le gana a `YPF`). Desempate alfabetico para ser determinista."""
    limpias = {_CLASE_RE.sub("", v).strip() for v in variantes}
    return sorted(limpias, key=lambda v: (-len(v), v))[0]


def main(dry: bool) -> int:
    with SessionLocal() as s:
        ons = s.scalars(select(InstrumentORM).where(InstrumentORM.sheet == SHEET)).all()

        grupos: dict[str, set[str]] = collections.defaultdict(set)
        for o in ons:
            sn = (o.short_name or "").strip()
            if sn and sn != o.ticker:
                grupos[raiz(ALIAS.get(sn.upper(), sn))].add(sn)

        canon = {r: canonico(v) for r, v in grupos.items()}
        cambios = []
        for o in ons:
            sn = (o.short_name or "").strip()
            if not sn or sn == o.ticker:
                continue
            nuevo = canon[raiz(ALIAS.get(sn.upper(), sn))]
            if nuevo != sn:
                cambios.append((o, sn, nuevo))

        por_destino: dict[str, list[str]] = collections.defaultdict(list)
        for o, viejo, nuevo in cambios:
            por_destino[nuevo].append(f"{o.ticker} ({viejo})")

        print(f"ON: {len(ons)}  |  emisores distintos: {len(grupos)}  "
              f"|  filas a renombrar: {len(cambios)}")
        for nuevo, items in sorted(por_destino.items()):
            print(f"\n  -> {nuevo}")
            for it in sorted(items):
                print(f"       {it}")

        if dry:
            print("\n== DRY RUN (no escribe) ==")
        else:
            backup_db(settings.catalog_db, settings.backup_dir,
                      keep=settings.backup_keep, tag="pre-on-normalize")
            for o, _, nuevo in cambios:
                o.short_name = nuevo
            s.commit()
            print(f"\nrenombrados: {len(cambios)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--dry-run" in sys.argv))
