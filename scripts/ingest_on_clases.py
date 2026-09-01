"""Carga la CLASE de las ON y AUDITA vencimiento (y cupon) contra el catalogo.

La clase ("Clase XXXIX", "Serie IV Clase A") es display-only y vive en
`raw_fields.serie_clase`. El panel ON la muestra en su columna; sin ella la fila
queda con un guion y no se distingue una serie de otra del mismo emisor.

NO corrige el cronograma: si el vencimiento o el cupon declarados difieren de los
que tiene el catalogo, lo REPORTA. Un vto o un cupon distinto cambia la TIR, asi
que la correccion se decide a mano y no se aplica en silencio.

DOS FUENTES, UN MOTOR: el informe IAMC trae el vencimiento en una columna
dd/mm/aaaa; el listado de YPF lo trae junto al cupon en texto ("Sep-2033 (1,50% /
7,00%)"). Esa es TODA la diferencia, asi que lo unico intercambiable es el parser
de la fila (`Formato`) — antes eran dos scripts clonados que divergian de a poco.

Match por ticker base o por la pata MEP. Idempotente. Snapshot pre-op VERIFICADO
(`op_guards.guard_write`): si el server esta vivo o el backup fallo, no escribe.

    py -3.12 scripts/ingest_on_clases.py --dry-run
    py -3.12 scripts/ingest_on_clases.py [ruta.csv]
    py -3.12 scripts/ingest_on_clases.py --force    # saltea los guards
"""
from __future__ import annotations

import csv
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import noload  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

# El MISMO parser que alimenta Instrument.coupon_rate: reusarlo garantiza que la
# auditoria compare contra el cupon que efectivamente muestra la app, y no contra
# una segunda interpretacion del campo crudo.
from core.infrastructure.db.catalog_repository import _coupon_pct  # noqa: E402
from core.infrastructure.db.engine import SessionLocal  # noqa: E402
from core.infrastructure.db.models import InstrumentORM  # noqa: E402
from scripts.op_guards import guard_write  # noqa: E402

DEFAULT = ROOT / "data" / "iamc" / "on_clases_2026_08.csv"

_MES = {"ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AGO": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12}

# Tolerancia de la comparacion de cupon: 1bp absorbe el redondeo de la fuente
# (declara 2 decimales) sin tapar una diferencia real de carga.
TOL_CUPON = 0.011


# --------------------------------------------------------------------------- #
# Fila normalizada + los dos formatos de fuente
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FilaClase:
    """Fila de la fuente ya normalizada: lo que las dos fuentes tienen en comun.

    `vto` (fecha exacta) y `vto_mes` (solo año/mes) son excluyentes: el IAMC declara
    el dia, YPF solo el mes. `vto_ilegible` guarda el texto crudo cuando la fuente
    declaro algo que no parsea — esa fila SE ESCRIBE igual, pero no se puede auditar
    y el reporte tiene que decirlo por separado."""
    ticker: str
    clase: str
    ticker_mep: str = ""
    vto: date | None = None
    vto_mes: tuple[int, int] | None = None
    vto_ilegible: str = ""
    cupones: list[float] = field(default_factory=list)


Formato = Callable[[dict], FilaClase]


def _fecha(txt: str) -> date | None:
    """dd/mm/aaaa -> date. None si viene incompleta (ej. '27/02/20**')."""
    try:
        return datetime.strptime((txt or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def fila_iamc(f: dict) -> FilaClase:
    """Formato del informe IAMC: ticker + pata MEP + vencimiento dd/mm/aaaa."""
    declarado = (f.get("vencimiento") or "").strip()
    vto = _fecha(declarado)
    return FilaClase(
        ticker=(f.get("ticker") or "").strip(),
        ticker_mep=(f.get("ticker_mep") or "").strip(),
        clase=(f.get("clase") or "").strip(),
        vto=vto,
        vto_ilegible=declarado if (vto is None and declarado) else "",
    )


def parse_vto_cupon(txt: str) -> tuple[tuple[int, int] | None, list[float]]:
    """'Sep-2033 (1,50% / 7,00%)' -> ((2033, 9), [1.5, 7.0]). Cupon puede faltar."""
    ym = None
    m = re.match(r"\s*([A-Za-z]{3})-(\d{4})", txt or "")
    if m and m.group(1).upper() in _MES:
        ym = (int(m.group(2)), _MES[m.group(1).upper()])
    cupones = [float(x.replace(",", ".")) for x in re.findall(r"(\d+[,.]\d+)\s*%", txt or "")]
    return ym, cupones


def fila_ypf(f: dict) -> FilaClase:
    """Formato YPF: vencimiento y cupon juntos en texto ('Sep-2033 (1,50% / 7,00%)').

    Los tickers de esta fuente son las patas MEP (…D) y la fila del catalogo es la
    base (…O), asi que el ticker se ofrece tambien como pata MEP para el match."""
    tk = (f.get("ticker") or "").strip()
    ym, cupones = parse_vto_cupon(f.get("vto_cupon") or "")
    return FilaClase(ticker=tk, ticker_mep=tk, clase=(f.get("clase") or "").strip(),
                     vto_mes=ym, cupones=cupones)


FORMATO_IAMC: Formato = fila_iamc
FORMATO_YPF: Formato = fila_ypf


# --------------------------------------------------------------------------- #
# Auditoria (no corrige nada: solo informa)
# --------------------------------------------------------------------------- #
def diff_cupon(actual: float | None, cupones: list[float]) -> str | None:
    """Compara el cupon del catalogo contra los que declara la fuente. None = OK.

    EL CUPON SALE DE `raw_fields["cupon anual %"]`, no de `spread_rate`:
    `spread_rate` es el SPREAD SOBRE TAMAR (ver core/domain/models.py) y esta en
    NULL en las 197 ON, asi que auditar contra el era codigo muerto que SIEMPRE
    reportaba "no hay diferencias" — le dio un OK falso al operador.

    CRITERIO STEP-UP: la fuente declara la ESCALERA COMPLETA ("1,50% / 7,00%") y el
    catalogo guarda UN solo cupon anual, que segun como se cargo puede ser el tramo
    vigente o el final. Por eso se acepta que coincida con CUALQUIER tramo declarado
    y solo hay diferencia si no coincide con ninguno. Coincidir con un tramo que no
    es el primero no es un error de carga, pero cambia el devengado -> se avisa.

    Sin cupon en el catalogo tampoco se puede auditar: se dice, en vez de callarlo
    (que era justamente lo que hacia parecer que todo coincidia)."""
    if not cupones:
        return None
    declarados = " / ".join(str(c) for c in cupones)
    if actual is None:
        return f"sin cupon cargado - no se pudo auditar (declarado {declarados})"
    tramo = next((i for i, c in enumerate(cupones) if abs(actual - c) <= TOL_CUPON), None)
    if tramo is None:
        return f"cupon catalogo {actual} != declarado {declarados}"
    if tramo > 0:
        return (f"cupon catalogo {actual} = tramo {tramo + 1} del step-up "
                f"({declarados}) - no el inicial")
    return None


def diff_vto(actual: date | None, fila: FilaClase) -> str | None:
    """Compara el vencimiento del catalogo contra el declarado. None = OK.

    Con `vto_mes` la fuente solo declara año/mes: se compara a esa granularidad, que
    es la maxima que permite el dato (comparar contra el dia 1 daria falsos)."""
    if actual is None:
        return None
    if fila.vto and actual != fila.vto:
        return f"vto catalogo {actual} != declarado {fila.vto}"
    if fila.vto_mes and (actual.year, actual.month) != fila.vto_mes:
        return (f"vto catalogo {actual} != declarado "
                f"{fila.vto_mes[1]:02d}/{fila.vto_mes[0]}")
    return None


# --------------------------------------------------------------------------- #
# Escritura
# --------------------------------------------------------------------------- #
def indexar_instrumentos(s, claves: set[str]) -> dict[str, InstrumentORM]:
    """Indice {ticker base | pata MEP} -> ORM, en UNA sola query.

    Antes el SELECT vivia DENTRO del loop: ~170 round-trips y, como
    `InstrumentORM.cashflows` es lazy='selectin', cada hit ademas arrastraba el
    cronograma completo del bono. `noload` corta esa segunda query: este script
    solo toca `raw_fields`, no mira flujos. Mismo patron que backfill_on_emisor.

    El ticker base gana sobre la pata MEP de otra fila (`setdefault`), asi el
    match es determinista y no depende del orden que devuelva la DB."""
    filas = s.scalars(
        select(InstrumentORM)
        .options(noload(InstrumentORM.cashflows))
        .where(InstrumentORM.ticker.in_(claves) | InstrumentORM.ticker_mep.in_(claves))
    ).all()
    idx: dict[str, InstrumentORM] = {o.ticker: o for o in filas}
    for o in filas:
        if o.ticker_mep:
            idx.setdefault(o.ticker_mep, o)
    return idx


def main(dry: bool, ruta: Path, formato: Formato = FORMATO_IAMC, force: bool = False) -> int:
    # Preflight ANTES de leer nada: sin red de seguridad (o con el monitor vivo,
    # que seguiria sirviendo el catalogo cacheado) no se escribe. El dry-run no
    # toca la DB, asi que se saltea el guard a proposito.
    if not dry and (rc := guard_write("pre-on-clases", force=force)):
        return rc

    with open(ruta, encoding="utf-8", newline="") as fh:
        crudas = list(csv.DictReader(fh))
    filas = [formato(f) for f in crudas]
    print(f"fuente: {ruta.name} ({len(filas)} filas)\n")

    with SessionLocal() as s:
        idx = indexar_instrumentos(
            s, {t for f in filas for t in (f.ticker, f.ticker_mep) if t})

        # Tres cubetas DISTINTAS, porque significan cosas distintas para el operador:
        #   salteadas   -> NO se escribieron (fila mal armada en la fuente)
        #   faltantes   -> NO se escribieron (el ticker no esta en el catalogo)
        #   sin_auditar -> SI se escribieron, pero el vto declarado es ilegible y la
        #                  auditoria no pudo correr sobre ellas.
        # Antes las dos ultimas se mezclaban bajo "filas descartadas", que listaba
        # como salteadas filas que en realidad habian entrado a la DB.
        escritos, salteadas, faltantes, sin_auditar, avisos = 0, [], [], [], []
        for f in filas:
            tk, clase = f.ticker, f.clase
            if not tk or not clase or clase == tk or clase.rstrip("DC") == tk.rstrip("O"):
                # La clase no puede ser el propio ticker: fila mal armada en la fuente.
                salteadas.append(tk or "?")
                continue

            o = idx.get(tk) or idx.get(f.ticker_mep)
            if o is None:
                faltantes.append(tk)
                continue

            if f.vto_ilegible:
                sin_auditar.append(f"{o.ticker} (vto '{f.vto_ilegible}')")
            for d in (diff_vto(o.maturity_date, f),
                      diff_cupon(_coupon_pct(o.raw_fields), f.cupones)):
                if d:
                    avisos.append(f"  {o.ticker}: {d}")

            rf = dict(o.raw_fields or {})
            if rf.get("serie_clase") != clase:
                rf["serie_clase"] = clase
                if not dry:
                    o.raw_fields = rf   # MERGE: el blob lo comparten cupon/sector/ley
                    flag_modified(o, "raw_fields")
                escritos += 1

        print(f"clases a escribir : {escritos}")
        if salteadas:
            print(f"filas salteadas, NO se escriben ({len(salteadas)}): {salteadas}")
        if faltantes:
            print(f"no estan en el catalogo ({len(faltantes)}): {faltantes}")

        print(f"\n--- auditoria contra la fuente ({len(avisos)} diferencias) ---")
        for a in avisos or ["  (ninguna: coinciden con el catalogo)"]:
            print(a)
        if sin_auditar:
            print(f"\nESCRITAS SIN AUDITAR - vto ilegible en la fuente ({len(sin_auditar)}):")
            for x in sin_auditar:
                print(f"  {x}")

        if dry:
            print("\n== DRY RUN (no escribe) ==")
        else:
            s.commit()
            print(f"\nescritos: {escritos}")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    raise SystemExit(main("--dry-run" in sys.argv, Path(args[0]) if args else DEFAULT,
                          force="--force" in sys.argv))
