"""Migración de datos: reclasifica los bonos con `instrument_type` HUÉRFANO.

Un `instrument_type` que no pertenece a ningún grupo de `core/domain/instrument_groups`
deja al bono INVISIBLE: todo el read-path (paneles, `apps/web/app.py::_ALL_TYPES`,
`on_service`) filtra por igualdad EXACTA de tipo, así que la fila se carga, guarda
cashflows y acumula precio, pero nunca se precia ni se muestra.

Origen del daño (reproducido): los ingest del IAMC guardaron `raw_fields` SIN la
clave `tipo`; un `save_instrument` posterior (el que dispara
`backfill_legs_from_universe` al completar las patas MEP/CABLE) reconstruyó el
instrumento desde ESE blob y `build_instrument` cayó al nombre de la hoja →
"OBLIGACIONES_NEGOCIABLES" / "SOBERANOS". El mismo round-trip perdió
`maturity_date`, `emission_date`, `category` y degradó `day_count` a ACT/365.25.

En la `catalog.db` viva del 2026-09-03: 43 ONs + BPOA8 (127 especies con sus patas
de moneda, ~17,5% del universo de renta fija).

El fix de código (`repositories._resolve_instrument_type` + el guard del ABM) evita
que ENTREN filas nuevas así; este script arregla las que ya están.

Fuente autoritativa (la misma con la que se cargaron):
  · ONs        → `data/iamc/on_data_2026_08_28.py`  (ONS[ticker]: tipo/emision/vto)
  · Soberanos  → `data/iamc/specs_2026_08_28.json`  (instrument_type/day_count/fechas)

FORWARD-ONLY: solo hace UPDATE de columnas existentes sobre las filas huérfanas
(nunca DROP, nunca DELETE, nunca re-seed). Idempotente por CONTENIDO: una fila con
tipo ya válido no entra al plan, así que correrlo dos veces no cambia nada.

    py -3.12 scripts/migrate_orphan_types.py            # DRY RUN (default)
    py -3.12 scripts/migrate_orphan_types.py --apply    # escribe (backup pre-op)
    py -3.12 scripts/migrate_orphan_types.py --apply --force   # con el server vivo
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_IAMC = ROOT / "data" / "iamc"
if str(_IAMC) not in sys.path:
    sys.path.insert(0, str(_IAMC))

from core.domain.instrument_groups import is_known_type  # noqa: E402
from op_guards import guard_write  # noqa: E402

# Identidad de la migración: se sella en `schema_meta` para auditoría (qué corrió y
# cuándo). NO reemplaza la idempotencia por contenido — el sello es informativo.
MIGRATION_ID = "2026-09-03-orphan-instrument-types"

# Tipo del informe IAMC → tipo canónico del catálogo. Mismo mapeo que
# `scripts/ingest_on_iamc_2026_08.py:152` (DL y ZC = dollar-linked).
_ON_TYPE = {"HD": "HARD DOLLAR", "DL": "DOLLAR LINKED", "ZC": "DOLLAR LINKED"}

_ON_DAY_COUNT = "ACT/365"                    # convención ON del catálogo (agents.md)
_ON_CATEGORY = "Obligaciones Negociables"

# Campos que el round-trip destruyó y que la semilla puede reponer. Se comparan
# contra lo que hay en la DB y solo se escribe lo que difiere.
_FIELDS = ("instrument_type", "maturity_date", "emission_date", "day_count", "category")


def _d(s) -> Optional[date]:
    if not s:
        return None
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def _load_on_seed() -> Dict[str, dict]:
    try:
        import on_data_2026_08_28 as on_data
    except ImportError:                                   # pragma: no cover
        return {}
    return {k.upper(): v for k, v in getattr(on_data, "ONS", {}).items()}


def _load_sob_seed() -> Dict[str, dict]:
    path = _IAMC / "specs_2026_08_28.json"
    if not path.is_file():                                # pragma: no cover
        return {}
    with open(path, encoding="utf-8") as f:
        return {k.upper(): v for k, v in json.load(f).items()}


def target_fields(ticker: str) -> Optional[Dict[str, Any]]:
    """Valores autoritativos para un ticker huérfano, o None si ninguna semilla
    lo conoce (en ese caso el script NO adivina: lo reporta y lo deja intacto)."""
    tk = (ticker or "").upper().strip()
    on = _load_on_seed().get(tk)
    if on:
        tipo = _ON_TYPE.get(str(on.get("tipo", "")).upper().strip())
        if tipo:
            return {"instrument_type": tipo,
                    "maturity_date": _d(on.get("vto")),
                    "emission_date": _d(on.get("emision")),
                    "day_count": _ON_DAY_COUNT,
                    "category": _ON_CATEGORY}
    spec = _load_sob_seed().get(tk)
    if spec:
        tipo = str(spec.get("instrument_type") or "").upper().strip()
        if tipo and is_known_type(tipo):
            return {"instrument_type": tipo,
                    "maturity_date": _d(spec.get("maturity_date")),
                    "emission_date": _d(spec.get("emission_date")),
                    "day_count": spec.get("day_count") or None,
                    "category": None}
    return None


def _orphans(rows) -> List[Any]:
    return [o for o in rows if not is_known_type(getattr(o, "instrument_type", ""))]


def build_plan(rows) -> List[Dict[str, Any]]:
    """Plan de cambios: [{ticker, tickers, sheet, changes:{campo:(viejo,nuevo)}}].

    Solo entran filas con tipo huérfano Y con semilla conocida. Un campo entra al
    plan únicamente si el valor nuevo aporta algo y difiere del actual — nunca se
    pisa un dato existente con None."""
    plan: List[Dict[str, Any]] = []
    for o in _orphans(rows):
        tgt = target_fields(getattr(o, "ticker", ""))
        if not tgt:
            continue
        changes: Dict[str, tuple] = {}
        for f in _FIELDS:
            new = tgt.get(f)
            if new is None:
                continue                      # la semilla no aporta → no degradar
            old = getattr(o, f, None)
            if old != new:
                changes[f] = (old, new)
        if not changes:
            continue
        tickers = [t for t in (getattr(o, "ticker", None), getattr(o, "ticker_mep", None),
                               getattr(o, "ticker_ccl", None)) if t]
        plan.append({"ticker": o.ticker, "tickers": tickers,
                     "sheet": getattr(o, "sheet", "") or "", "changes": changes})
    plan.sort(key=lambda e: e["ticker"])
    return plan


def unresolved(rows) -> List[str]:
    """Huérfanos que NINGUNA semilla resuelve → hay que tipificarlos por ABM."""
    return sorted(o.ticker for o in _orphans(rows) if not target_fields(o.ticker))


def _fmt(v) -> str:
    # Sin glifos fuera de cp1252: la consola de Windows los rompe (UnicodeEncodeError).
    return "(vacio)" if v in (None, "") else str(v)


def print_plan(plan, missing) -> None:
    if not plan:
        print("Nada que migrar: ningún bono con instrument_type huérfano.")
    for e in plan:
        print(f"  {e['ticker']:<7} [{' / '.join(e['tickers'])}]  hoja {e['sheet']}")
        for f, (old, new) in e["changes"].items():
            print(f"      {f:<16} {_fmt(old)}  ->  {_fmt(new)}")
    if missing:
        print(f"\nSIN FUENTE ({len(missing)}) — quedan como están, tipificalos por ABM:")
        print("  " + " ".join(missing))


def _stamp(s) -> None:
    """Sella la migración en `schema_meta` (auditoría; no gatea la idempotencia)."""
    s.execute(
        __import__("sqlalchemy").text(
            "INSERT INTO schema_meta (key, value) VALUES (:k, :v) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"),
        {"k": f"migration:{MIGRATION_ID}", "v": datetime.now().isoformat(timespec="seconds")},
    )


def apply_migration() -> int:
    """Aplica el plan en UNA transacción. Devuelve cuántos bonos se tocaron.
    Sin backup ni guards (los pone `main`) — pensado para tests y para reuso."""
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    init_db()
    with SessionLocal.begin() as s:
        rows = s.execute(select(InstrumentORM)).scalars().all()
        plan = build_plan(rows)
        by_ticker = {o.ticker: o for o in rows}
        for e in plan:
            o = by_ticker[e["ticker"]]
            for f, (_old, new) in e["changes"].items():
                setattr(o, f, new)
            # `tipo` también al blob del form: sin esto el próximo round-trip del
            # ABM (get_instrument → save_instrument) volvería a perderlo. MERGE,
            # nunca reemplazo (el blob lleva origen/ley/cupón de la semilla).
            raw = dict(o.raw_fields or {})
            raw["tipo"] = o.instrument_type
            o.raw_fields = raw
            flag_modified(o, "raw_fields")
        if plan:
            _stamp(s)
    return len(plan)


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
        missing = unresolved(rows)
        especies = sum(len(e["tickers"]) for e in plan)

    print(f"migración {MIGRATION_ID}")
    print(f"bonos con instrument_type huérfano y fuente conocida: {len(plan)} "
          f"({especies} especies con sus patas de moneda)\n")
    print_plan(plan, missing)

    if not apply:
        print("\n== DRY RUN (no escribe). Para aplicar: --apply ==")
        return 0
    if not plan:
        return 0

    rc = guard_write("pre-orphan-types", force=force)
    if rc:
        return rc
    n = apply_migration()
    print(f"\nOK: {n} bono(s) reclasificados. Reiniciá el server para verlos en los paneles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
