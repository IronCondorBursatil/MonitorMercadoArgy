"""CAFCI mutual-fund (FCI) data with on-disk persistence.

Architectural exception: market prices come from Data912, but Argentine mutual
fund (FCI) returns are published only by CAFCI (Cámara Argentina de Fondos
Comunes de Inversión).

The historical `fedemoglia/cafci-api` approach (hit `api.cafci.org.ar` with no
auth) is dead: that host now sits behind a CloudFront route allowlist that
answers ``{"error":"Route not allowed"}`` (403) to every path. The live public
source is the *estadísticas* micrositio, whose comparador endpoint bundles —
in a single daily-refreshed JSON — both the full fund catalog and the daily
returns matrix for every priced class:

    https://estadisticas.cafci.org.ar/comparador-de-fondos.json
      catalogo : { fondos: [ {id, nombre, tipo_renta, moneda, sociedad_gerente,
                              dias_liquidacion, valuacion, clases:[{id, nombre,
                              moneda, ...}] } ] }      (~1149 fondos / 4602 clases)
      matriz   : { fecha_base, generated_at,
                   clases: { "<clase_id>": { valor_cuotaparte, fecha_valor,
                              dias_7:{tna,directo}, mes_1:{...}, dias_90, dias_180,
                              ytd, meses_12 } } }       (~3723 clases con precio)

We join `catalogo` (metadata) with `matriz` (daily returns) on `clase_id`.

Resilience (mirrors `BCRAIndicesProvider`): the parsed dataset is mirrored to
``data/history/cafci_diario.json``. On startup the cache hydrates from disk;
CAFCI is queried at most once per successful day (with a short retry cooldown
on failure). If CAFCI is unreachable the project keeps serving the last
persisted snapshot — only data freshness degrades.
"""

import json
import logging
import os
import threading
import time
import unicodedata
from datetime import date
from typing import Any, Dict, List, Optional

from config.settings import DATA_DIR
from core.infrastructure._http import http_get_json

logger = logging.getLogger(__name__)


_CAFCI_URL  = "https://estadisticas.cafci.org.ar/comparador-de-fondos.json"
_CAFCI_JSON = os.path.join(DATA_DIR, "history", "cafci_diario.json")

# Periods present in matriz.clases[*] (no 1-day point upstream — shortest is 7d).
_PERIODS = ("dias_7", "mes_1", "dias_90", "dias_180", "ytd", "meses_12")

# Cooldown between failed fetch attempts so spamming filters while CAFCI is down
# doesn't hammer the upstream (success gates for the rest of the day instead).
_RETRY_COOLDOWN_S = 60.0

# ArgentinaDatos FCI — fallback cuando estadisticas.cafci.org.ar está caído
# y no hay snapshot en disco. Schema: [{fondo, tipo, fecha, vcp, ccp, patrimonio, horizonte}]
# Cubre las 5 categorías estándar (exluye 'otros' y 'variables' que tienen schema distinto).
_ARD_FCI_CATEGORIAS = {
    "Mercado de Dinero": "https://argentinadatos.com/v1/finanzas/fci/mercadoDinero/ultimo",
    "Renta Variable":    "https://argentinadatos.com/v1/finanzas/fci/rentaVariable/ultimo",
    "Renta Fija":        "https://argentinadatos.com/v1/finanzas/fci/rentaFija/ultimo",
    "Renta Mixta":       "https://argentinadatos.com/v1/finanzas/fci/rentaMixta/ultimo",
    "Retorno Total":     "https://argentinadatos.com/v1/finanzas/fci/retornoTotal/ultimo",
}


def _to_float(v: Any) -> Optional[float]:
    """CAFCI returns numbers as strings ("230993.537"). Parse leniently."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm(s: Any) -> str:
    """Lowercase + strip accents for accent-insensitive name search."""
    if s is None:
        return ""
    t = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def _parse_payload(payload: dict) -> dict:
    """Join catalogo (metadata) + matriz (daily returns) into a flat record list.

    Only classes present in `matriz` (i.e. with a current valuation) are kept —
    closed/unpriced classes have catalog metadata but no returns.
    """
    catalogo = payload.get("catalogo") or {}
    matriz = payload.get("matriz") or {}
    fondos = catalogo.get("fondos") or []
    rend_by_clase = matriz.get("clases") or {}

    funds: List[dict] = []
    for f in fondos:
        tipo_renta = (f.get("tipo_renta") or {}).get("nombre")
        soc = (f.get("sociedad_gerente") or {}).get("nombre")
        fondo_moneda = f.get("moneda") or {}
        for cl in (f.get("clases") or []):
            cid = str(cl.get("id"))
            rend = rend_by_clase.get(cid)
            if not rend:
                continue
            moneda = (cl.get("moneda") or fondo_moneda or {}).get("nombre")
            funds.append({
                "fondo_id": f.get("id"),
                "clase_id": cl.get("id"),
                "fondo_nombre": f.get("nombre"),
                "clase_nombre": cl.get("nombre"),
                "tipo_renta": tipo_renta,
                "moneda": moneda,
                "sociedad": soc,
                "tipo_dinero": f.get("tipo_dinero"),
                "dias_liquidacion": f.get("dias_liquidacion"),
                "valuacion": f.get("valuacion"),
                "vcp": _to_float(rend.get("valor_cuotaparte")),
                "fecha_valor": rend.get("fecha_valor"),
                "rend": {
                    p: {
                        "tna": _to_float((rend.get(p) or {}).get("tna")),
                        "directo": _to_float((rend.get(p) or {}).get("directo")),
                    }
                    for p in _PERIODS
                },
            })

    meta = {
        "fecha_base": matriz.get("fecha_base"),
        "generated_at": matriz.get("generated_at"),
        "total": len(funds),
        "periodos": list(_PERIODS),
    }
    return {"meta": meta, "funds": funds}


def _save_json(path: str, data: dict) -> None:
    """Persist the parsed dataset atomically (.tmp + os.replace)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"Could not persist {path}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def _load_json(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("funds"), list):
            return data
    except (OSError, ValueError) as e:
        logger.warning(f"Could not read {path}: {e}")
    return None


class CAFCIProvider:
    """FCI catalog + daily returns from CAFCI, hydrated from disk on startup."""

    _lock = threading.Lock()
    _dataset: dict = {"meta": {}, "funds": []}
    _last_attempt: Optional[date] = None     # date of last *successful* load
    _last_fail_ts: float = 0.0               # monotonic ts of last failed fetch
    _disk_loaded: bool = False

    @classmethod
    def _hydrate_from_disk(cls) -> None:
        data = _load_json(_CAFCI_JSON)
        if data:
            cls._dataset = data
            logger.info(
                "Loaded CAFCI from disk: %d fund classes (fecha_base=%s).",
                len(data.get("funds", [])), (data.get("meta") or {}).get("fecha_base"),
            )
        cls._disk_loaded = True

    @classmethod
    def _fetch_ard_fallback(cls) -> List[dict]:
        """Fallback desde ArgentinaDatos FCI cuando CAFCI y el disco fallan.

        Fetchea las 5 categorías estándar en paralelo. Devuelve fondos con vcp
        pero sin rendimientos por período (rend = None) — degradado pero funcional.
        Solo se activa si el dataset está vacío (no hay snapshot en disco).
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        funds: List[dict] = []

        def _fetch_cat(item):
            tipo_renta, url = item
            try:
                rows = http_get_json(
                    url, timeout=10, user_agent="balanz-monitor/1.0",
                    source=f"ARD/FCI/{tipo_renta}",
                )
                if not isinstance(rows, list):
                    return []
                result = []
                for row in rows:
                    nombre = row.get("fondo") or ""
                    if not nombre:
                        continue
                    result.append({
                        "fondo_id":        None,
                        "clase_id":        None,
                        "fondo_nombre":    nombre,
                        "clase_nombre":    None,
                        "tipo_renta":      tipo_renta,
                        "moneda":          "ARS",
                        "sociedad":        None,
                        "tipo_dinero":     None,
                        "dias_liquidacion": None,
                        "valuacion":       None,
                        "vcp":             _to_float(row.get("vcp")),
                        "fecha_valor":     row.get("fecha"),
                        "rend": {p: {"tna": None, "directo": None} for p in _PERIODS},
                    })
                return result
            except Exception as e:
                logger.warning("ARD FCI/%s fallback failed: %s", tipo_renta, e)
                return []

        with ThreadPoolExecutor(max_workers=5) as ex:
            for partial in ex.map(_fetch_cat, list(_ARD_FCI_CATEGORIAS.items())):
                funds.extend(partial)

        return funds

    def _ensure_loaded(self) -> None:
        with self._lock:
            if not self._disk_loaded:
                self._hydrate_from_disk()
            if self._last_attempt == date.today():
                return
            now = time.monotonic()
            if self._last_fail_ts and (now - self._last_fail_ts) < _RETRY_COOLDOWN_S:
                # En cooldown: si no hay datos en disco, intentar ARD como último recurso.
                if not type(self)._dataset.get("funds"):
                    self._apply_ard_fallback()
                return
            try:
                payload = http_get_json(
                    _CAFCI_URL, timeout=30, user_agent="Mozilla/5.0",
                    source="CAFCI/comparador",
                )
                parsed = _parse_payload(payload)
            except Exception as e:
                type(self)._last_fail_ts = now
                logger.warning(f"CAFCI fetch failed: {e}")
                # Sin disco: ARD como fallback inmediato.
                if not type(self)._dataset.get("funds"):
                    self._apply_ard_fallback()
                return
            if parsed["funds"]:
                type(self)._dataset = parsed
                type(self)._last_attempt = date.today()
                _save_json(_CAFCI_JSON, parsed)
                logger.info(
                    "CAFCI: %d fund classes loaded (fecha_base=%s).",
                    len(parsed["funds"]), parsed["meta"].get("fecha_base"),
                )
            else:
                type(self)._last_fail_ts = now
                logger.warning("CAFCI: payload parsed to 0 classes — keeping prior snapshot.")

    def _apply_ard_fallback(self) -> None:
        """Llama al fallback ARD y actualiza el dataset en memoria (sin persistir a disco)."""
        ard_funds = self._fetch_ard_fallback()
        if ard_funds:
            type(self)._dataset = {
                "meta": {
                    "fecha_base": None, "generated_at": None,
                    "total": len(ard_funds), "periodos": list(_PERIODS),
                    "source": "argentinadatos_fallback",
                },
                "funds": ard_funds,
            }
            logger.info(
                "FCI: sirviendo %d fondos desde ArgentinaDatos (fallback, sin rendimientos).",
                len(ard_funds),
            )

    # ------------------------------------------------------------------ #
    # Public accessors
    # ------------------------------------------------------------------ #

    def get_meta(self) -> dict:
        """Snapshot meta + filter option lists (with counts) for the UI."""
        self._ensure_loaded()
        funds = self._dataset.get("funds", [])
        meta = dict(self._dataset.get("meta", {}))

        def _options(key: str) -> List[dict]:
            counts: Dict[str, int] = {}
            for f in funds:
                v = f.get(key)
                if v:
                    counts[v] = counts.get(v, 0) + 1
            return [{"value": k, "count": counts[k]} for k in sorted(counts)]

        meta["tipo_renta_options"] = _options("tipo_renta")
        meta["moneda_options"] = _options("moneda")
        return meta

    def list_funds(
        self,
        tipo_renta: Optional[str] = None,
        moneda: Optional[str] = None,
        query: Optional[str] = None,
        sort: str = "mes_1",
        direction: str = "desc",
        metric: str = "tna",
        limit: Optional[int] = None,
    ) -> List[dict]:
        """Filtered + sorted fund list. Sort is by `metric` (tna|directo) of the
        `sort` period; funds missing that value sink to the bottom regardless of
        direction."""
        self._ensure_loaded()
        funds = self._dataset.get("funds", [])

        if sort not in _PERIODS:
            sort = "mes_1"
        if metric not in ("tna", "directo"):
            metric = "tna"

        q = _norm(query) if query else None
        out = []
        for f in funds:
            if tipo_renta and f.get("tipo_renta") != tipo_renta:
                continue
            if moneda and f.get("moneda") != moneda:
                continue
            if q and q not in _norm(f.get("fondo_nombre")) \
                    and q not in _norm(f.get("clase_nombre")) \
                    and q not in _norm(f.get("sociedad")):
                continue
            out.append(f)

        reverse = direction != "asc"

        def _key(f):
            v = (f.get("rend", {}).get(sort) or {}).get(metric)
            # Missing values always last: pair a presence flag with the value so
            # ordering stays correct for both asc and desc.
            if v is None:
                return (1, 0.0)
            return (0, -v if reverse else v)

        out.sort(key=_key)
        if limit and limit > 0:
            out = out[:limit]
        return out

    def get_fund(self, clase_id) -> Optional[dict]:
        self._ensure_loaded()
        try:
            cid = int(clase_id)
        except (TypeError, ValueError):
            return None
        for f in self._dataset.get("funds", []):
            if f.get("clase_id") == cid:
                return f
        return None
