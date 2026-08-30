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
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from config.settings import settings
# Helpers/endpoints viven en core.domain.fci (fuente única): _to_float parsea los
# números-string de CAFCI; _ARD_FCI_CATEGORIAS son los endpoints del fallback ArgentinaDatos.
from core.domain.fci import ARD_FCI_ENDPOINTS as _ARD_FCI_CATEGORIAS
from core.domain.fci.derive import to_float as _to_float
import httpx

logger = logging.getLogger(__name__)

_AR_TZ = timezone(timedelta(hours=-3))


def _ar_today() -> date:
    """Fecha de hoy en hora Argentina (UTC-3). El gate diario usa esta fecha para
    que el rollover ocurra a medianoche AR, evitando saltear la publicación CAFCI."""
    return datetime.now(tz=_AR_TZ).date()


_CAFCI_URL  = "https://estadisticas.cafci.org.ar/comparador-de-fondos.json"
_CAFCI_JSON = os.path.join(str(settings.history_dir), "cafci_diario.json")

# Periods present in matriz.clases[*] (no 1-day point upstream — shortest is 7d).
_PERIODS = ("dias_7", "mes_1", "dias_90", "dias_180", "ytd", "meses_12")

# Cooldown between failed fetch attempts so spamming filters while CAFCI is down
# doesn't hammer the upstream (success gates for the rest of the day instead).
_RETRY_COOLDOWN_S = 60.0


def _parse_payload(payload: dict) -> dict:
    """Join catalogo (metadata) + matriz (daily returns) into a flat per-class record.

    Conserva, además de los campos base, los **campos ricos** que CAFCI ya manda en el
    mismo request y que antes se descartaban: comisiones (`honorarios` → fee_admin/in/out),
    `inversion_minima`, `horizonte`, `duration`, `region`, `objetivo`, `sociedad_depositaria`,
    `inicio`, tickers ISIN/Bloomberg, `mm_puro`/`mm_indice`. Los usa el panel FCI (detalle/ficha).

    Solo se conservan clases con valuación vigente (presentes en `matriz`) de fondos con
    estado activo (`estado in {1, None}`); cerradas/inactivas se descartan.
    """
    catalogo = payload.get("catalogo") or {}
    matriz = payload.get("matriz") or {}
    fondos = catalogo.get("fondos") or []
    rend_by_clase = matriz.get("clases") or {}

    funds: List[dict] = []
    for f in fondos:
        if f.get("estado") not in (1, "1", None):
            continue
        tipo_renta = (f.get("tipo_renta") or {}).get("nombre")
        soc = (f.get("sociedad_gerente") or {}).get("nombre")
        dep = (f.get("sociedad_depositaria") or {}).get("nombre")
        region = (f.get("region") or {}).get("nombre")
        horizonte = (f.get("horizonte") or {}).get("nombre")
        duration = (f.get("duration") or {}).get("nombre")
        fondo_moneda = f.get("moneda") or {}
        for cl in (f.get("clases") or []):
            cid = str(cl.get("id"))
            rend = rend_by_clase.get(cid)
            if not rend:
                continue
            moneda = (cl.get("moneda") or fondo_moneda or {}).get("nombre")
            hon = cl.get("honorarios") or {}
            fee_admin = sum(
                x for x in (_to_float(hon.get("administracion_gerente")),
                            _to_float(hon.get("administracion_depositaria")),
                            _to_float(hon.get("gasto_ordinario_gestion"))) if x is not None
            )
            funds.append({
                "fondo_id": f.get("id"),
                "clase_id": cl.get("id"),
                "fondo_nombre": f.get("nombre"),
                "clase_nombre": cl.get("nombre"),
                "tipo_renta": tipo_renta,
                "moneda": moneda,
                "sociedad": soc,
                "depositaria": dep,
                "tipo_dinero": f.get("tipo_dinero"),
                "dias_liquidacion": f.get("dias_liquidacion"),
                "valuacion": f.get("valuacion"),
                "region": region,
                "horizonte": horizonte,
                "duration": duration,
                "mm_puro": f.get("mm_puro"),
                "mm_indice": f.get("mm_indice"),
                "objetivo": f.get("objetivo"),
                "inicio": f.get("inicio"),
                "fee_admin": round(fee_admin, 4),
                "fee_in": _to_float(hon.get("ingreso")),
                "fee_out": _to_float(hon.get("rescate")),
                "inversion_minima": cl.get("inversion_minima"),
                "ticker_isin": cl.get("ticker_isin"),
                "ticker_bloomberg": cl.get("ticker_bloomberg"),
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
        from concurrent.futures import ThreadPoolExecutor

        funds: List[dict] = []

        def _fetch_cat(item):
            tipo_renta, url = item
            try:
                resp = httpx.get(url, timeout=10.0, headers={"User-Agent": "balanz-monitor/1.0"})
                resp.raise_for_status()
                rows = resp.json()
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
            if self._last_attempt == _ar_today():
                return
            now = time.monotonic()
            if self._last_fail_ts and (now - self._last_fail_ts) < _RETRY_COOLDOWN_S:
                # En cooldown: si no hay datos en disco, intentar ARD como último recurso.
                if not type(self)._dataset.get("funds"):
                    self._apply_ard_fallback()
                return
            try:
                resp = httpx.get(_CAFCI_URL, timeout=30.0, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                parsed = _parse_payload(resp.json())
            except Exception as e:
                type(self)._last_fail_ts = now
                logger.warning(f"CAFCI fetch failed: {e}")
                # Sin disco: ARD como fallback inmediato.
                if not type(self)._dataset.get("funds"):
                    self._apply_ard_fallback()
                return
            if parsed["funds"]:
                type(self)._dataset = parsed
                type(self)._last_attempt = _ar_today()
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

    # Read-path del panel FCI: `apps/web/fci_service.py` llama `_ensure_loaded()` y
    # lee `_dataset` directo (build_fci_dataset). La vieja API SSR del provider
    # (get_meta / list_funds / get_fund) se retiró por no usarse.
