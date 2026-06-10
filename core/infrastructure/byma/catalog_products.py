"""Familias de productos BYMA para el catálogo (open API, sin token).

Datos de REFERENCIA (no el hot-path de pricing): cauciones (curva de tasas) y
SENEBI-ON (universo ON bilateral) se traen on-demand de los paneles open
`/cauciones` y `/senebi-obligaciones-negociables`; los índices MERVAL/BURCAP salen
del endpoint chart (`chart_history.fetch_index_history`). Todo best-effort y con
cache TTL en memoria (la reference data cambia lento; no martillar BYMA por view).

Las funciones `fetch_*` son puras (cliente httpx inyectable → testeables sin red);
los wrappers `*_cached` agregan el TTL y los usa el router.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

import httpx

from core.infrastructure.byma.sources import BymaOpenSource

logger = logging.getLogger(__name__)

_BASE = BymaOpenSource.BASE
_HEADERS = {
    "User-Agent": "Mozilla/5.0", "Accept": "application/json",
    "Content-Type": "application/json",
}


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows(payload) -> list:
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data") or []
    return payload if isinstance(payload, list) else []


def _post(path: str, body: dict, client: Optional[httpx.Client]) -> list:
    own = client is None
    cli = client or httpx.Client(verify=False, timeout=40.0)
    try:
        r = cli.post(_BASE + path, json=body, headers=_HEADERS)
        if r.status_code != 200:
            return []
        return _rows(r.json())
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.debug("byma %s falló: %s", path, e)
        return []
    finally:
        if own:
            cli.close()


# --------------------------------------------------------------------------- #
# Cauciones — curva de tasas de muy corto plazo. El panel cotiza tasa/factor en
# `settlementPrice`/`closingPrice`; surfaceamos plazo (días) + tasa + moneda.
# --------------------------------------------------------------------------- #
def fetch_cauciones(client: Optional[httpx.Client] = None) -> List[dict]:
    out: List[dict] = []
    for d in _post("/cauciones", {}, client):
        sym = d.get("symbol")
        if not sym:
            continue
        out.append({
            "symbol": sym,
            "underlying": d.get("underlyingSymbol") or "",
            "ccy": d.get("denominationCcy") or "",
            "days": d.get("daysToMaturity"),
            "maturity": (d.get("maturityDate") or "")[:10],
            "rate": _f(d.get("settlementPrice")) or _f(d.get("closingPrice"))
            or _f(d.get("trade")),
            "volume": _f(d.get("volumeAmount")),
        })
    out.sort(key=lambda x: (x["days"] is None, x["days"] or 0, x["ccy"]))
    return out


# --------------------------------------------------------------------------- #
# SENEBI-ON — universo ON bilateral (~3160). Útil para descubrir ONs nuevas.
# --------------------------------------------------------------------------- #
def fetch_senebi_on(client: Optional[httpx.Client] = None) -> List[dict]:
    out: List[dict] = []
    for d in _post("/senebi-obligaciones-negociables", {"page_size": 5000}, client):
        sym = d.get("symbol")
        if not sym:
            continue
        out.append({
            "symbol": sym,
            "ccy": d.get("denominationCcy") or "",
            "maturity": (d.get("maturityDate") or "")[:10],
            "days": d.get("daysToMaturity"),
            "last": _f(d.get("closingPrice")) or _f(d.get("trade")),
        })
    out.sort(key=lambda x: x["symbol"])
    return out


# --------------------------------------------------------------------------- #
# Ficha técnica rica (de raw_fields['byma']['ficha'], poblada por catalog_enrich).
# --------------------------------------------------------------------------- #
def ficha_for(key: str) -> dict:
    """`raw_fields['byma']['ficha']` del instrumento que matchea `key` (cualquier
    pata ticker O el ISIN), o {}."""
    t = (key or "").upper().strip()
    if not t:
        return {}
    from sqlalchemy import or_, select

    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    init_db()
    with SessionLocal() as s:
        o = s.execute(select(InstrumentORM).where(or_(
            InstrumentORM.ticker == t, InstrumentORM.ticker_mep == t,
            InstrumentORM.ticker_ccl == t, InstrumentORM.isin == t,
        ))).scalars().first()
    if o is None:
        return {}
    return ((o.raw_fields or {}).get("byma") or {}).get("ficha") or {}


# --------------------------------------------------------------------------- #
# Cache TTL en memoria (reference data → no refetch por view).
# --------------------------------------------------------------------------- #
_CACHE: Dict[str, tuple] = {}


def _cached(key: str, ttl: float, fn):
    now = time.monotonic()
    ent = _CACHE.get(key)
    if ent and ent[0] > now:
        return ent[1]
    val = fn()
    if val:                                  # no cachear vacíos (reintenta la próxima)
        _CACHE[key] = (now + ttl, val)
    return val


def cauciones_cached(ttl: float = 90.0) -> List[dict]:
    return _cached("cauciones", ttl, fetch_cauciones)


def senebi_on_cached(ttl: float = 1800.0) -> List[dict]:
    return _cached("senebi_on", ttl, fetch_senebi_on)


# --------------------------------------------------------------------------- #
# Índices — panel live `/index-price` (16 índices S&P/BYMA). El sparkline es una
# FRANJA DE 5 RUEDAS densificada con acumulación intradía (ver index_history.py):
# M/G se anclan con sus cierres diarios del chart; los 16 acumulan el valor de
# `/index-price` en cada poll. Persistente → se llena durante la rueda y entre días.
# --------------------------------------------------------------------------- #
_INDEX_ORDER = {"M": 0, "G": 1, "SPMERVDT": 2}  # los headline primero
_SPARK_MAX = 160                                 # cap de puntos del sparkline (downsample)
_MG_PRIMED = False                               # backfill chart M/G 1× por proceso


def fetch_index_prices(client: Optional[httpx.Client] = None) -> List[dict]:
    """Valor actual de los índices BYMA (`/index-price`): código, nombre oficial,
    último, cierre previo y variación %."""
    out: List[dict] = []
    for d in _post("/index-price", {}, client):
        code = d.get("symbol")
        if not code:
            continue
        last = _f(d.get("price")) or _f(d.get("trade")) or _f(d.get("closingPrice"))
        prev = _f(d.get("previousClosingPrice"))
        var = _f(d.get("variation"))          # fracción (0.0104 = +1.04%)
        chg = (var * 100.0 if var is not None
               else ((last / prev - 1.0) * 100.0 if (last and prev) else None))
        out.append({"code": code, "name": d.get("description") or code,
                    "last": last, "prev": prev, "change_pct": chg})
    return out


def _downsample(vals: List[float], k: int) -> List[float]:
    """Reduce `vals` a ~k puntos (conserva el último). Para que el sparkline no
    crezca sin límite cuando se acumulan muchos ticks en las 5 ruedas."""
    n = len(vals)
    if n <= k or k < 2:
        return vals
    step = n / k
    return [vals[int(i * step)] for i in range(k - 1)] + [vals[-1]]


def index_snapshot(*, prices_fetch=None, store=None, now=None, ruedas=None,
                   prime: bool = True) -> List[dict]:
    """Todos los índices del panel live, ordenados (MERVAL/General primero), con el
    sparkline de la FRANJA DE 5 RUEDAS (`points`) del store persistente. Acumula el
    tick actual de los 16 y, una vez por proceso, ancla M/G (chart) + cierre previo
    de los 16. `prices_fetch()`, `store`, `now`, `ruedas` inyectables para tests."""
    import datetime as _dt

    from config.settings import settings
    from core.infrastructure.byma.index_history import (
        get_index_history_store, prime_from_chart, window_start)

    global _MG_PRIMED
    if prices_fetch is None:
        prices_fetch = fetch_index_prices
    if ruedas is None:
        ruedas = settings.index_ruedas
    store = store or get_index_history_store()
    now = now or _dt.datetime.now()
    today = now.date()
    ts = now.replace(microsecond=0).isoformat()

    prices = prices_fetch() or []

    if prime and not _MG_PRIMED:
        try:
            prime_from_chart(store, ruedas=ruedas)              # anclas diarias M/G
        except Exception:  # noqa: BLE001 — best-effort
            pass
        yday = (today - _dt.timedelta(days=1)).isoformat() + "T17:00:00"
        store.record_many([(ix["code"], yday, ix["prev"])      # ancla cierre previo (16)
                           for ix in prices if ix.get("prev") is not None])
        _MG_PRIMED = True

    since = window_start(today, ruedas)
    store.prune(since)
    ser = store.series_since(since)
    # Acumular el tick actual de los 16 SOLO si cambió vs la última muestra (evita
    # puntos planos redundantes off-hours; densifica al moverse el índice).
    new: List[Tuple[str, str, float]] = []
    for ix in prices:
        last = ix.get("last")
        if last is None:
            continue
        prev_pts = ser.get((ix["code"] or "").upper())
        if not prev_pts or prev_pts[-1][1] != float(last):
            new.append((ix["code"], ts, float(last)))
    if new:
        store.record_many(new)
        for c, t, v in new:
            ser.setdefault(str(c).upper(), []).append((t, v))

    out: List[dict] = []
    for ix in prices:
        pts = [v for _, v in ser.get((ix["code"] or "").upper(), [])]
        if len(pts) < 2 and ix.get("prev") is not None and ix.get("last") is not None:
            pts = [float(ix["prev"]), float(ix["last"])]       # fallback mínimo
        out.append({**ix, "points": _downsample(pts, _SPARK_MAX)})
    out.sort(key=lambda x: (_INDEX_ORDER.get(x["code"], 9), x["name"]))
    return out
