"""Universo de especies BYMA: ingesta del seed `titulos_final.csv` a la tabla
`byma_catalog` + búsqueda (ticker/ISIN/emisor/categoría) para la pestaña ABM.

Tabla de REFERENCIA navegable, separada del catálogo de pricing (`instruments`):
se puede borrar y reingerir sin pérdida. Una fila por símbolo cotizante (~6.4k).
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy import delete, func, or_, select

from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import BymaCatalogORM

logger = logging.getLogger(__name__)

# securityType → categoría cuando la especie no trae tipoEspecie (sin ficha).
_SECTYPE_CAT = {
    "CS": "Acciones", "CD": "Cedears", "CORP": "Obligaciones Negociables",
    "GO": "Títulos Públicos", "FUT": "Futuros", "EXTERNAL": "Acciones Internacionales",
}


def _categoria(tipo_especie: str, security_type: str, panel: str) -> str:
    """Categoría mostrable: tipoEspecie si viene; si no, se infiere del securityType
    (o del panel para índices/totales). Espeja la lógica del pipeline BYMA."""
    if tipo_especie:
        return tipo_especie
    cat = _SECTYPE_CAT.get(security_type, "")
    if cat:
        return cat
    if "ndice" in (panel or "") or "Indice" in (panel or ""):
        return "Índices"
    if "Total" in (panel or ""):
        return "Totales"
    return security_type or "Otros"


def ingest_byma_catalog(csv_path: Optional[Path] = None) -> int:
    """Carga `byma_catalog` desde el CSV (delete + insert, idempotente). Devuelve
    cuántas filas quedaron. 0 si el CSV no existe."""
    if csv_path is None:
        from config.settings import settings
        csv_path = settings.byma_catalog_csv
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        logger.info("byma_catalog: seed no encontrado en %s (omitido).", csv_path)
        return 0

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: List[dict] = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f, delimiter=";"):
            sym = (r.get("symbol") or "").upper().strip()
            if not sym:
                continue
            sec = (r.get("securityType") or "").strip()
            rows.append({
                "symbol": sym,
                "ticker_pesos": (r.get("ticker_pesos") or "").strip() or None,
                "isin": (r.get("codigoIsin") or "").strip() or None,
                "categoria": _categoria((r.get("tipoEspecie") or "").strip(), sec,
                                        (r.get("panel") or "").strip()),
                "moneda": (r.get("moneda") or "").strip() or None,
                "security_type": sec or None,
                "clase_liquidacion": (r.get("clase_liquidacion") or "").strip() or None,
                "cotiza": 1 if str(r.get("cotiza")).strip().lower() in ("true", "1") else 0,
                "segmento": (r.get("segmento") or "").strip() or None,
                "panel": (r.get("panel") or "").strip() or None,
                "ins_type": (r.get("insType") or "").strip() or None,
                "emisor": (r.get("emisor") or "").strip() or None,
                "sector": None,  # futuro: ficha sociedad (actividad)
                "updated_at": ts,
            })

    init_db()
    with SessionLocal.begin() as s:
        s.execute(delete(BymaCatalogORM))
        if rows:
            s.bulk_insert_mappings(BymaCatalogORM, rows)
    logger.info("byma_catalog: %d especies ingeridas.", len(rows))
    return len(rows)


def _to_dict(o: BymaCatalogORM) -> dict:
    return {
        "symbol": o.symbol, "ticker_pesos": o.ticker_pesos, "isin": o.isin,
        "categoria": o.categoria, "moneda": o.moneda, "security_type": o.security_type,
        "cotiza": o.cotiza, "segmento": o.segmento, "panel": o.panel,
        "ins_type": o.ins_type, "emisor": o.emisor, "sector": o.sector,
    }


def search_byma_catalog(q: str = "", categoria: str = "", *, limit: int = 200,
                        offset: int = 0) -> Tuple[List[dict], int]:
    """Busca especies por texto (symbol/ISIN/emisor/ticker_pesos, LIKE) + filtro de
    categoría. Devuelve (filas, total). Ordena por símbolo."""
    init_db()
    q = (q or "").strip()
    categoria = (categoria or "").strip()
    conds = []
    if q:
        like = f"%{q}%"
        conds.append(or_(
            BymaCatalogORM.symbol.ilike(like),
            BymaCatalogORM.isin.ilike(like),
            BymaCatalogORM.emisor.ilike(like),
            BymaCatalogORM.ticker_pesos.ilike(like),
        ))
    if categoria:
        conds.append(BymaCatalogORM.categoria == categoria)

    with SessionLocal() as s:
        total = s.execute(select(func.count()).select_from(BymaCatalogORM).where(*conds)).scalar_one()
        rows = s.execute(
            select(BymaCatalogORM).where(*conds)
            .order_by(BymaCatalogORM.symbol).limit(limit).offset(offset)
        ).scalars().all()
        return [_to_dict(o) for o in rows], int(total)


# moneda (BYMA) → columna. El universo solo trae estas 3 monedas de liquidación.
_MONEDA_SLOT = {"ARS": "pesos", "MEP": "mep", "cable": "cable"}


def _empty_cells() -> dict:
    return {"pesos": [], "mep": [], "cable": []}


def _ficha_isins() -> set:
    """ISINs (upper) de instrumentos del catálogo curado que YA tienen ficha técnica
    rica (`raw_fields['byma']['ficha']`). Una consulta liviana (~cientos de filas)."""
    from core.infrastructure.db.models import InstrumentORM
    out: set = set()
    with SessionLocal() as s:
        for isin, raw in s.execute(
                select(InstrumentORM.isin, InstrumentORM.raw_fields)).all():
            if isin and ((raw or {}).get("byma") or {}).get("ficha"):
                out.add(isin.upper())
    return out


def _loaded_ids() -> Tuple[set, set]:
    """(ISINs, tickers) que YA están en el catálogo de **pricing** (`instruments`) →
    el título está dado de alta y se le calcula TIR/MD/etc. Match por ISIN o por
    cualquiera de sus 3 patas (primario/MEP/CABLE). Consulta liviana."""
    from core.infrastructure.db.models import InstrumentORM
    isins: set = set()
    tickers: set = set()
    with SessionLocal() as s:
        for isin, tk, mep, ccl in s.execute(select(
                InstrumentORM.isin, InstrumentORM.ticker,
                InstrumentORM.ticker_mep, InstrumentORM.ticker_ccl)).all():
            if isin:
                isins.add(isin.upper())
            for t in (tk, mep, ccl):
                if t:
                    tickers.add(t.upper())
    return isins, tickers


def _legislacion(isin: Optional[str]) -> str:
    """Ley aplicable inferida del prefijo de país del ISIN (ON): AR→local, US→NY.
    Otros prefijos (XS internacional) o sin ISIN → '' (la tabla muestra '—')."""
    p = (isin or "")[:2].upper()
    if p == "AR":
        return "Ley Local"
    if p == "US":
        return "Ley NY"
    return ""


def search_byma_grouped(q: str = "", categoria: str = "", *, limit: int = 200,
                        offset: int = 0) -> Tuple[List[dict], int]:
    """Universo BYMA consolidado: UNA fila por **título valor** (no por especie).

    Agrupa por ISIN (clave del activo; fallback a ticker_pesos / symbol cuando no
    hay ISIN) y reparte cada especie en su columna por moneda (ARS=pesos / MEP /
    cable), separando la clase de liquidación `primary` de las `especial`/`otro`
    (estas últimas "aún sin utilidad"). Una búsqueda que pega en CUALQUIER pata
    trae el grupo entero. Devuelve (grupos, total_de_grupos), ordenado por ticker
    base, **paginado por grupo**.

    Cada grupo: {key, base, isin, categoria, security_type, panel, emisor,
    primary:{pesos,mep,cable}, especial:{pesos,mep,cable}} — celdas como string
    (varias especies en la misma celda se unen con ' · ')."""
    init_db()
    q = (q or "").strip().upper()
    categoria = (categoria or "").strip()

    with SessionLocal() as s:
        stmt = select(BymaCatalogORM)
        if categoria:  # categoria es consistente dentro de un ISIN → no parte grupos
            stmt = stmt.where(BymaCatalogORM.categoria == categoria)
        rows = s.execute(stmt).scalars().all()

    groups: dict = {}
    order: List[str] = []
    for o in rows:
        key = (o.isin or o.ticker_pesos or o.symbol or "").upper()
        g = groups.get(key)
        if g is None:
            g = {
                "key": key,
                "base": (o.ticker_pesos or o.symbol or "").upper(),
                "isin": o.isin, "legislacion": _legislacion(o.isin),
                "categoria": o.categoria,
                "security_type": o.security_type, "panel": o.panel, "emisor": o.emisor,
                "primary": _empty_cells(), "especial": _empty_cells(),
                "_match": [],
            }
            groups[key] = g
            order.append(key)
        slot = _MONEDA_SLOT.get(o.moneda)
        if slot:
            bucket = "primary" if o.clase_liquidacion == "primary" else "especial"
            g[bucket][slot].append((o.symbol or "").upper())
        for v in (o.symbol, o.isin, o.emisor, o.ticker_pesos):
            if v:
                g["_match"].append(v.upper())

    items = [groups[k] for k in order]
    if q:
        items = [g for g in items if any(q in t for t in g["_match"])]
    items.sort(key=lambda g: (g["base"], g["key"]))
    total = len(items)
    page = items[offset:offset + limit]
    # Marcar los grupos cuyo ISIN ya tiene ficha técnica rica (→ ticker naranja en la
    # UI) y los que YA están cargados en el catálogo de pricing. Solo para la página
    # visible (no para los miles de grupos).
    ficha_isins = _ficha_isins() if page else set()
    loaded_isins, loaded_tickers = _loaded_ids() if page else (set(), set())
    for g in page:
        g.pop("_match", None)
        g["has_ficha"] = bool(g.get("isin") and g["isin"].upper() in ficha_isins)
        # ¿Cargado en pricing? — por ISIN o por cualquier pata (antes de unir las celdas).
        legs = {t.upper() for bucket in ("primary", "especial")
                for slot in ("pesos", "mep", "cable") for t in g[bucket][slot]}
        g["loaded"] = bool((g.get("isin") and g["isin"].upper() in loaded_isins)
                           or (legs & loaded_tickers))
        for bucket in ("primary", "especial"):
            for slot in ("pesos", "mep", "cable"):
                g[bucket][slot] = " · ".join(sorted(set(g[bucket][slot])))
    return page, total


def categories() -> List[Tuple[str, int]]:
    """[(categoría, cantidad)] para los chips del filtro, desc por cantidad."""
    init_db()
    with SessionLocal() as s:
        rows = s.execute(
            select(BymaCatalogORM.categoria, func.count())
            .group_by(BymaCatalogORM.categoria)
            .order_by(func.count().desc())
        ).all()
    return [(c or "—", int(n)) for c, n in rows]


def count() -> int:
    init_db()
    with SessionLocal() as s:
        return int(s.execute(select(func.count()).select_from(BymaCatalogORM)).scalar_one())
