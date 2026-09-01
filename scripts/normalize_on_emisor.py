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
ALIAS (curado a mano, entrada por entrada) cubre lo que la raiz NO puede resolver:
la marca comercial contra la razon social que inyecto el backfill BYMA — `EDENOR`
vs `Empresa Distribuidora y Comercializadora Norte S.A.`, `YPF LUZ` vs `YPF Energia
Electrica S.A.`. Es la unica puerta a una fusion que la raiz no justifica, y por eso
se amplia solo verificando la ficha del emisor.

Idempotente. Snapshot pre-op VERIFICADO (`op_guards.guard_write`): si el server
esta vivo o el backup fallo, no renombra nada.

    py -3.12 scripts/normalize_on_emisor.py --dry-run
    py -3.12 scripts/normalize_on_emisor.py
    py -3.12 scripts/normalize_on_emisor.py --force   # saltea los guards
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

from core.infrastructure.db.engine import SessionLocal  # noqa: E402
from core.infrastructure.db.models import InstrumentORM  # noqa: E402
from scripts.op_guards import guard_write  # noqa: E402

SHEET = "Obligaciones_Negociables"

# Marca comercial -> razon social. Valvula de escape para lo que la raiz NO puede
# unificar sola: el backfill BYMA (`backfill_on_emisor.py`) inyecto las razones
# sociales LARGAS del Universo junto a los nombres cortos que ya estaban cargados, y
# no comparten raiz porque no son la misma cadena ("EDENOR" vs "Empresa Distribuidora
# y Comercializadora Norte S.A."). Sigue sin ser matcheo difuso: cada entrada se
# agrega A MANO contra la ficha del emisor — eso es lo que evita fusionar creditos
# distintos (ver YPF S.A. vs YPF Energia Electrica en el docstring de arriba).
# La clave se busca por RAIZ, no por el string crudo, asi que una sola entrada cubre
# todas las variantes sufijadas: "EDENOR", "EDENOR - Clase 9" y "Edenor S.A.".
ALIAS: dict[str, str] = {
    "EDENOR": "Empresa Distribuidora y Comercializadora Norte S.A.",
    "EDEMSA": "Empresa Distribuidora de Electricidad de Mendoza S.A.",
    "IRSA": "IRSA INVERSIONES Y REPRESENTACIONES S.A.",
    "MASTELLONE": "Mastellone Hermanos S.A.",
    "PAN AMERICAN ENERGY": "PAN AMERICAN ENERGY, S.L. SUCURSAL ARGENTINA",
    "VISTA ENERGY": "VISTA ENERGY ARGENTINA S.A.U.",
    "YPF LUZ": "YPF Energía Eléctrica S.A.",
}

_CLASE_RE = re.compile(r"\s*[-–]\s*Clase\b.*$", re.IGNORECASE)
# Sigla societaria que quedo descosida al borrar los puntos ("S A I C" -> "SAIC").
# Solo al final del nombre y exigiendo >=2 letras sueltas seguidas: asi no toca un
# emisor que legitimamente termine en una inicial.
_SIGLA_RE = re.compile(r"(?:\b[A-Z]\s+)+[A-Z]\b\s*$")
# Formas societarias YA sin puntos ni espacios internos (asi las deja _SIGLA_RE).
# El `\s+` inicial es lo que impide amputar una palabra: NEWSAN no es NEW + SA.
_FORMAS_SOC = ("SACIFIA", "SACIFI", "SACIF", "SACI", "SAICF", "SAIC",
               "SAU", "SAS", "SRL", "SCA", "SA")
_SOC_RE = re.compile(r"\s+(?:" + "|".join(_FORMAS_SOC) + r")$")


def _raiz_bruta(nombre: str) -> str:
    """Raiz textual, SIN resolver ALIAS (la envuelve `raiz`)."""
    s = _CLASE_RE.sub("", nombre or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    # Los puntos se BORRAN, no se cambian por espacio: "S.R.L." tiene que quedar
    # "SRL" para que _SOC_RE lo reconozca. El bug viejo era ese — con " " quedaba
    # "S R L", solo "S.A." sobrevivia limpio y el emisor se partia en dos grupos.
    s = s.replace(",", "").replace(".", "")
    s = re.sub(r"\s+", " ", s).strip()
    # La fuente tambien escribe la sigla espaciada ("ARCOR S A I C") -> "ARCOR SAIC".
    s = _SIGLA_RE.sub(lambda m: m.group(0).replace(" ", ""), s)
    return _SOC_RE.sub("", s).strip()


# Indice del ALIAS por raiz, armado una sola vez. La resolucion es de UN salto (una
# razon social destino nunca es a su vez clave) para que `raiz` sea idempotente: con
# cadenas, correr el script dos veces renombraria en ping-pong.
_ALIAS_POR_RAIZ: dict[str, str] = {_raiz_bruta(k): v for k, v in ALIAS.items()}


def raiz(nombre: str) -> str:
    """Clave de agrupamiento: sin serie, sin acentos, sin forma societaria y con la
    marca comercial ya resuelta a su razon social (ALIAS)."""
    base = _raiz_bruta(nombre)
    destino = _ALIAS_POR_RAIZ.get(base)
    return _raiz_bruta(destino) if destino else base


def canonico(variantes: set[str]) -> str:
    """La variante mas completa del grupo: sin '- Clase', y a igualdad la mas larga
    (asi `YPF S.A.` le gana a `YPF`). Desempate alfabetico para ser determinista."""
    limpias = {_CLASE_RE.sub("", v).strip() for v in variantes}
    return sorted(limpias, key=lambda v: (-len(v), v))[0]


def main(dry: bool, force: bool = False) -> int:
    # Preflight ANTES de leer nada: un rename masivo sin red de seguridad (o por
    # debajo de un monitor vivo, que seguiria mostrando los nombres viejos) no
    # corre. El dry-run no escribe, asi que se saltea el guard a proposito.
    if not dry and (rc := guard_write("pre-on-normalize", force=force)):
        return rc

    with SessionLocal() as s:
        ons = s.scalars(select(InstrumentORM).where(InstrumentORM.sheet == SHEET)).all()

        grupos: dict[str, set[str]] = collections.defaultdict(set)
        for o in ons:
            sn = (o.short_name or "").strip()
            if sn and sn != o.ticker:
                grupos[raiz(sn)].add(sn)   # `raiz` ya resuelve ALIAS (por raiz)

        canon = {r: canonico(v) for r, v in grupos.items()}
        cambios = []
        for o in ons:
            sn = (o.short_name or "").strip()
            if not sn or sn == o.ticker:
                continue
            nuevo = canon[raiz(sn)]
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
            for o, _, nuevo in cambios:
                o.short_name = nuevo
            s.commit()
            print(f"\nrenombrados: {len(cambios)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--dry-run" in sys.argv, force="--force" in sys.argv))
