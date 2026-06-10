"""Enriquecimiento del catálogo de títulos con datos de BYMA (ISIN + emisor + tipo).

Lee el catálogo BYMA `symbol → {codigoIsin, tipoEspecie, securityType, emisor}`
(seed `data/byma/titulos_final.csv`, generado por el pipeline de BYMADATA_EXCEL) y,
para cada instrumento ya cargado, matchea **cualquiera de sus patas** (ticker/
ticker_mep/ticker_ccl) contra el símbolo BYMA y le setea el `isin` (mismo ISIN para
todas las monedas del bono) + guarda emisor/tipo en `raw_fields["byma"]`.

Idempotente (solo escribe filas que cambian). NO crea filas nuevas (decisión:
enriquecer lo cargado, no importar el universo BYMA).
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Dict, Optional

from sqlalchemy import select

from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import InstrumentORM

logger = logging.getLogger(__name__)


def load_byma_catalog(csv_path: Path) -> Dict[str, dict]:
    """`{SYMBOL_UPPER: {isin, tipoEspecie, securityType, emisor}}` desde el CSV
    (delimitado por `;`, utf-8-sig). Filas sin ISIN se incluyen igual (isin='')."""
    out: Dict[str, dict] = {}
    if not Path(csv_path).is_file():
        return out
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f, delimiter=";"):
            sym = (r.get("symbol") or "").upper().strip()
            if not sym:
                continue
            out[sym] = {
                "isin": (r.get("codigoIsin") or "").strip(),
                "tipoEspecie": (r.get("tipoEspecie") or "").strip(),
                "securityType": (r.get("securityType") or "").strip(),
                "emisor": (r.get("emisor") or "").strip(),
            }
    return out


def _row_tickers(orm: InstrumentORM):
    return [t.upper() for t in (orm.ticker, orm.ticker_mep, orm.ticker_ccl) if t]


def enrich_isin_from_byma(csv_path: Optional[Path] = None) -> int:
    """Setea `isin` + `raw_fields['byma']` en los instrumentos que matchean el
    catálogo BYMA. Devuelve cuántas filas se modificaron. Idempotente."""
    if csv_path is None:
        from config.settings import settings
        csv_path = settings.byma_catalog_csv
    catalog = load_byma_catalog(Path(csv_path))
    if not catalog:
        logger.info("ISIN enrich: catálogo BYMA no encontrado en %s (omitido).", csv_path)
        return 0

    init_db()
    changed = 0
    with SessionLocal.begin() as s:
        rows = s.execute(select(InstrumentORM)).scalars().all()
        for o in rows:
            meta = next((catalog[t] for t in _row_tickers(o) if t in catalog), None)
            if not meta:
                continue
            isin = meta.get("isin") or None
            byma_meta = {k: v for k, v in meta.items() if k != "isin" and v}
            want_byma = byma_meta or None
            new_raw = dict(o.raw_fields or {})
            prev_byma = new_raw.get("byma")
            if o.isin == isin and prev_byma == want_byma:
                continue  # ya enriquecido → no reescribir (idempotente)
            o.isin = isin
            if want_byma is not None:
                new_raw["byma"] = want_byma
            else:
                new_raw.pop("byma", None)  # limpiar metadata BYMA obsoleta
            o.raw_fields = new_raw  # reasignar p/ que SQLAlchemy detecte el cambio
            changed += 1
    if changed:
        logger.info("ISIN enrich: %d instrumentos enriquecidos desde BYMA.", changed)
    return changed


# --------------------------------------------------------------------------- #
# Enriquecimiento por FICHA TÉCNICA en vivo (autoritativo) para los instrumentos
# del catálogo curado que quedaron SIN isin tras el seed. El set es chico (~cientos)
# → fetch confiable sin el throttling del bulk del pipeline. Llena AL30/DICP/etc.
# --------------------------------------------------------------------------- #
_FICHA_URL = ("https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free"
              "/bnown/fichatecnica/especies/general")
_FICHA_HEADERS = {
    "User-Agent": "Mozilla/5.0", "Accept": "application/json", "Content-Type": "application/json",
    "Token": "dc826d4c2dde7519e882a250359a23a7", "Options": "technical-details",
    "Origin": "https://open.bymadata.com.ar", "Referer": "https://open.bymadata.com.ar/",
}


def _ficha_raw(session, symbol: str):
    """`data[0]` crudo de la ficha técnica (dict con TODOS los campos), o None.
    Para acciones/opciones/tickers inexistentes la API devuelve `data: []` → None."""
    try:
        r = session.post(_FICHA_URL, json={"symbol": symbol}, timeout=20)
        data = (r.json() or {}).get("data") if r.status_code == 200 else None
        return data[0] if data else None
    except Exception:  # noqa: BLE001 — falla puntual → se reintenta el próximo arranque
        return None


def _ficha(session, symbol: str):
    """(codigoIsin, emisor, denominacion) de la ficha técnica, o (None,None,None)."""
    f = _ficha_raw(session, symbol) or {}
    return (f.get("codigoIsin") or None, f.get("emisor") or None,
            f.get("denominacion") or None)


# Campo crudo de la ficha BYMA → clave guardada en raw_fields['byma']['ficha'].
# Texto plano (amortizacion/interes) sirve de cross-check del schedule sintetizado;
# ley/moneda/montos/denom_minima son metadata útil para el ABM y el catálogo.
_FICHA_KEYS = {
    "ley": "ley", "moneda": "moneda",
    "formaAmortizacion": "amortizacion", "interes": "interes",
    "montoNominal": "monto_nominal", "montoResidual": "monto_residual",
    "denominacionMinima": "denom_minima", "tipoObligacion": "tipo_obligacion",
    "tipoGarantia": "garantia", "tipoEspecie": "tipo_especie",
    "default": "default", "paisLey": "pais_ley",
    "fechaEmision": "fecha_emision", "fechaVencimiento": "fecha_vencimiento",
}
_FICHA_DATE_KEYS = {"fecha_emision", "fecha_vencimiento"}


def _curate_ficha(d: dict) -> dict:
    """Subconjunto curado y limpio de la ficha cruda (descarta vacíos; fechas a
    'YYYY-MM-DD'). Función pura → testeable sin red."""
    out: dict = {}
    for src, dst in _FICHA_KEYS.items():
        v = d.get(src)
        if isinstance(v, str):
            v = v.strip()
            if dst in _FICHA_DATE_KEYS:
                v = v[:10]
        if v not in (None, "", [], {}):
            out[dst] = v
    return out


def enrich_ficha_meta(max_fetch: int = 800, fetch_fn=None) -> int:
    """Guarda los campos RICOS de la ficha técnica BYMA (ley/moneda/forma de
    amortización/interés/montos/denom. mínima) en `raw_fields['byma']['ficha']`
    de cada bono con ISIN que aún no los tenga. Idempotente (salta los ya
    enriquecidos), concurrente, best-effort (un fallo de red se reintenta el
    próximo arranque). `fetch_fn(symbol)->dict|None` inyectable para tests.

    Gateado a instrumentos con ISIN (≈ bonos/ON/letras): la ficha de acciones es
    vacía. Solo persiste fichas NO vacías → no late fallos transitorios."""
    own_session = fetch_fn is None
    if own_session:
        try:
            import truststore  # TLS de BYMA (cadena incompleta)
            truststore.inject_into_ssl()
        except ImportError:
            pass
        import requests
        session = requests.Session()
        session.headers.update(_FICHA_HEADERS)
        fetch_fn = lambda sym: _ficha_raw(session, sym)  # noqa: E731

    from concurrent.futures import ThreadPoolExecutor

    init_db()
    with SessionLocal() as s:
        rows = s.execute(
            select(InstrumentORM).where(InstrumentORM.isin.isnot(None))
        ).scalars().all()
        targets = []
        for o in rows:
            byma = (o.raw_fields or {}).get("byma") or {}
            if "ficha" in byma:                      # ya enriquecido → saltar
                continue
            legs = _row_tickers(o)
            if legs:
                targets.append((o.ticker, legs))
        targets = targets[:max_fetch]
    if not targets:
        return 0

    def _fetch(item):
        primary, legs = item
        for sym in legs[:3]:                         # variantes comparten ficha
            d = fetch_fn(sym)
            if d:
                cur = _curate_ficha(d)
                if cur:
                    return primary, cur
        return primary, None

    found: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for primary, cur in ex.map(_fetch, targets):
            if cur:
                found[primary] = cur
    if not found:
        return 0

    with SessionLocal.begin() as s:
        for primary, cur in found.items():
            o = s.get(InstrumentORM, primary)
            if o is None:
                continue
            raw = dict(o.raw_fields or {})
            byma = dict(raw.get("byma") or {})       # preserva emisor/tipo del seed
            byma["ficha"] = cur
            raw["byma"] = byma
            o.raw_fields = raw
    logger.info("Ficha meta: %d instrumentos enriquecidos (ley/amort/montos).", len(found))
    return len(found)


def enrich_isin_from_ficha(max_fetch: int = 600) -> int:
    """Para los instrumentos del catálogo curado SIN isin, busca el ISIN en la ficha
    técnica BYMA (autoritativo) probando cada pata. Concurrente. Devuelve cuántos se
    completaron. Requiere red (corre en to_thread al arranque); falla suave."""
    try:
        import truststore  # TLS de BYMA (cadena incompleta)
        truststore.inject_into_ssl()
    except ImportError:
        pass
    import requests
    from concurrent.futures import ThreadPoolExecutor

    init_db()
    with SessionLocal() as s:
        rows = s.execute(
            select(InstrumentORM).where(InstrumentORM.isin.is_(None))
        ).scalars().all()
        targets = [(o.ticker, _row_tickers(o)) for o in rows if _row_tickers(o)][:max_fetch]
    if not targets:
        return 0

    session = requests.Session()
    session.headers.update(_FICHA_HEADERS)

    def _fetch(item):
        primary, legs = item
        for sym in legs[:3]:               # variantes comparten ISIN → basta una
            isin, emisor, den = _ficha(session, sym)
            if isin:
                return primary, isin, emisor, den
        return primary, None, None, None

    found: Dict[str, tuple] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for primary, isin, emisor, den in ex.map(_fetch, targets):
            if isin:
                found[primary] = (isin, emisor, den)
    if not found:
        return 0

    with SessionLocal.begin() as s:
        for primary, (isin, emisor, den) in found.items():
            o = s.get(InstrumentORM, primary)
            if o is None or o.isin:
                continue
            o.isin = isin
            raw = dict(o.raw_fields or {})
            byma = dict(raw.get("byma") or {})
            if emisor:
                byma["emisor"] = emisor
            if den:
                byma["denominacion"] = den
            if byma:
                raw["byma"] = byma
                o.raw_fields = raw
    logger.info("ISIN enrich (ficha): %d instrumentos completados en vivo.", len(found))
    return len(found)
