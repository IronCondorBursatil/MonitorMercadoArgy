"""Store local persistente del histórico FCI por fondo (vcp/ccp/patrimonio) — la
base para computar el **flujo neto real** de cada fondo (suscripciones − rescates).

Por qué existe: el flujo de un FCI no se publica. Se infiere de la variación de las
**cuotapartes en circulación (ccp)**, que cambian *solo* por suscripciones/rescates,
no por el precio:

    flujo_neto(d) ≈ (ccp[d] − ccp[d-1]) · precio_cuotaparte[d]

(equivalente a ΔAUM − rendimiento·AUM, pero sin contaminarse por la apreciación del
VCP). CAFCI no trae `ccp`/`patrimonio`; **ArgentinaDatos sí**, pero solo del último
corte (es snapshot, sin histórico). Por eso acumulamos ese corte a diario nosotros
—mismo patrón que `price_history.py`— y a las pocas ruedas hay una serie real de
`ccp` para derivar flujos de TODO el universo, sin depender de fuentes pagas.

Persistencia: SQLite en `%LOCALAPPDATA%\\monitor` (fuera del working tree de git, que evita
SQLite mid-write). **Read-path** (`get_series`) 100% local con cache en memoria;
**write-path** (`record_from_ard`) corre en la task de mantenimiento diario (prod).
Bajo pytest los loops no arrancan → el store queda vacío (determinístico).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
from typing import Dict, List, Optional

from config.settings import settings
from core.domain.fci import ARD_FCI_ENDPOINTS as _ARD_FCI
from core.domain.fci.derive import norm as _norm, to_float as _f
import httpx

logger = logging.getLogger(__name__)

# _ARD_FCI: categoría → URL del último corte (schema {fondo, tipo, fecha, vcp, ccp,
# patrimonio, horizonte}). Fuente única en core.domain.fci. _norm/_f vienen de derive.


class FCIHistoryStore:
    """Histórico `{fondo: {date: {vcp, ccp, patrimonio}}}` sobre SQLite (`fci_history`).

    Thread-safe igual que PriceHistoryStore: conexión efímera por operación + un
    `RLock` que protege el cache en memoria (carga lazy, update incremental por write).
    El fondo se indexa normalizado (lowercase, sin acentos) para el join con CAFCI.
    """

    def __init__(self, db_path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, Dict[date, dict]]] = None

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS fci_history ("
            "  fondo      TEXT NOT NULL,"
            "  day        TEXT NOT NULL,"          # ISO 'YYYY-MM-DD'
            "  vcp        REAL,"
            "  ccp        REAL,"
            "  patrimonio REAL,"
            "  PRIMARY KEY (fondo, day))"
        )
        return con

    def _ensure_cache(self) -> None:
        if self._cache is not None:
            return
        cache: Dict[str, Dict[date, dict]] = {}
        try:
            with closing(self._connect()) as con:
                for fondo, day, vcp, ccp, pat in con.execute(
                        "SELECT fondo, day, vcp, ccp, patrimonio FROM fci_history"):
                    try:
                        cache.setdefault(fondo, {})[date.fromisoformat(day)] = {
                            "vcp": vcp, "ccp": ccp, "patrimonio": pat}
                    except (TypeError, ValueError):
                        continue
        except sqlite3.Error as e:
            # No latchear el cache vacío: un error transitorio de SQLite (lock/IO en
            # Windows) dejaría los flujos en blanco para todo el proceso aun con el
            # histórico ya en disco. Dejamos _cache=None → la próxima lectura reintenta.
            logger.warning("fci_history: load failed, will retry (%s)", e)
            return
        self._cache = cache

    def get_series(self, fondo: str) -> Dict[date, dict]:
        """`{date: {vcp, ccp, patrimonio}}` para un fondo (vacío si no hay). Copia bajo
        lock para que el read-path pueda iterarla mientras la task de fondo escribe."""
        k = _norm(fondo)
        with self._lock:
            self._ensure_cache()
            if self._cache is None:
                return {}
            return {d: dict(v) for d, v in self._cache.get(k, {}).items()}

    def record_snapshot(self, rows: List[dict], on_date: date) -> int:
        """Acumula un corte diario: filas `{fondo, vcp, ccp, patrimonio}`. Descarta
        las que no sirven para flujos (sin nombre o sin `ccp`). Idempotente por
        (fondo, día) — la última escritura del día pisa. Devuelve filas escritas."""
        clean = []
        for r in rows or ():
            fondo = _norm(r.get("fondo"))
            ccp = _f(r.get("ccp"))
            if not fondo or ccp is None:
                continue
            clean.append((fondo, on_date.isoformat(), _f(r.get("vcp")), ccp, _f(r.get("patrimonio"))))
        if not clean:
            return 0
        with self._lock:
            try:
                with closing(self._connect()) as con, con:
                    con.executemany(
                        "INSERT INTO fci_history (fondo, day, vcp, ccp, patrimonio) "
                        "VALUES (?,?,?,?,?) ON CONFLICT(fondo, day) DO UPDATE SET "
                        "vcp=excluded.vcp, ccp=excluded.ccp, patrimonio=excluded.patrimonio",
                        clean,
                    )
            except sqlite3.Error as e:
                logger.warning("fci_history: write failed (%s)", e)
                return 0
            self._ensure_cache()
            if self._cache is not None:
                for fondo, day, vcp, ccp, pat in clean:
                    self._cache.setdefault(fondo, {})[date.fromisoformat(day)] = {
                        "vcp": vcp, "ccp": ccp, "patrimonio": pat}
        return len(clean)

    def net_flows(self, fondo: str) -> Dict[date, float]:
        """Serie de flujo neto (Δccp × precio de cuotaparte) de un fondo desde su
        histórico acumulado. El precio sale de `patrimonio/ccp` de la fila, con fallback
        `vcp/1000` (ArgentinaDatos publica el VCP por 1.000 cuotapartes) — ver
        `_unit_price`/`net_flow_series`."""
        return net_flow_series(self.get_series(fondo))

    def keys(self) -> List[str]:
        """Nombres (normalizados) de los fondos acumulados — para agregar por fondo/gestora."""
        with self._lock:
            self._ensure_cache()
            return list(self._cache.keys()) if self._cache else []


# Guard de continuidad: un fondo no duplica/colapsa su circulación en una rueda.
# Saltos > este umbral (×) en el Δccp/ccp_prev se tratan como discontinuidades de
# fuente (renombre de fondo en ArgentinaDatos, error de carga, etc.) y se descartan.
_NET_FLOW_MAX_JUMP = 3.0   # Δccp > 3× ccp_prev → implausible


def _unit_price(row: dict) -> Optional[float]:
    """Precio de UNA cuotaparte a partir de la fila del store.

    ArgentinaDatos publica `vcp` **por cada 1.000 cuotapartes**: sobre el store real
    (cortes 2026-06-09..2026-08-31, 16.830 filas) la identidad `patrimonio = ccp·vcp/1000`
    se cumple en 9.276 de las 9.394 filas con los 3 campos no nulos (las 114 restantes
    son redondeo a entero de ccp/patrimonio en fondos minúsculos: 0,0025% del patrimonio
    del corte). Excepción real: los 2 fondos "investire … (valor de liq. final de cp)"
    publican `patrimonio = ccp·vcp` (valor por 1 cuotaparte). Por eso el precio se deriva
    por fila de `patrimonio/ccp` — que respeta ambas convenciones y deja el flujo en la
    misma escala que el AUM — y solo cae a `vcp/1000` cuando falta patrimonio o ccp
    (7.410 filas del store traen `patrimonio` 0/NULL y 7.434 traen `ccp` 0).
    """
    pat, ccp, vcp = row.get("patrimonio"), row.get("ccp"), row.get("vcp")
    if pat and ccp:
        return pat / ccp
    return vcp / 1000.0 if vcp is not None else None


def net_flow_series(series: Dict[date, dict]) -> Dict[date, float]:
    """Flujo neto orgánico entre puntos consecutivos: `(ccp[d] − ccp[prev]) · precio[d]`.

    Pura: no toca disco. Usa el precio unitario del día (no del previo, ver `_unit_price`:
    `patrimonio/ccp`, con fallback `vcp/1000` — ArgentinaDatos publica el VCP por 1.000
    cuotapartes) para valuar las suscriptas/rescatadas. El primer punto no tiene flujo.

    **`ccp <= 0` es DATO AUSENTE, no una circulación real de cero.** ArgentinaDatos
    publica `ccp = 0` (y `patrimonio` 0/NULL) cuando simplemente no trae el dato de esa
    clase ese día: en el store real son 2.122 de 4.706 filas del corte (45%), incluidos
    fondos de decenas de miles de millones. Tomarlos como observación fabricaba dos
    flujos fantasma simétricos:

      - `0 → X` se leía como una suscripción por el patrimonio ENTERO (40 transiciones
        en el store, +1,014e11), y el guard de continuidad NO la filtraba porque exige
        `ccp_prev > 0`;
      - `X → 0` se leía como el rescate total del fondo (37 transiciones, −8,27e10), y
        ese SÍ pasaba el guard (Δccp/ccp = 1,0 < 3,0).

    Por eso esos puntos se descartan y la serie se **puentea** (el flujo se imputa al
    siguiente día con dato, que es lo mismo que ya se hace con los días sin publicación).
    El alta REAL de un fondo no se pierde por esto: el primer punto con `ccp` de la serie
    nunca genera flujo (no hay previo contra el cual medir).

    Guard de continuidad: saltos implausibles (Δccp > _NET_FLOW_MAX_JUMP × ccp_prev)
    se descartan — evita que un renombre de fondo en ArgentinaDatos genere un flujo
    espurio gigante en el panel FCI.
    """
    days = sorted(d for d, v in series.items()
                  if v and (v.get("ccp") or 0) > 0 and v.get("vcp") is not None)
    out: Dict[date, float] = {}
    for i in range(1, len(days)):
        prev, cur = series[days[i - 1]], series[days[i]]
        ccp_prev = prev["ccp"]
        delta_ratio = abs(cur["ccp"] - ccp_prev) / ccp_prev
        if delta_ratio > _NET_FLOW_MAX_JUMP:
            logger.warning(
                "net_flow_series: salto implausible Δccp/ccp=%.1f× el %s — "
                "descartado (¿renombre/colisión de fondo en ArgentinaDatos?)",
                delta_ratio, days[i])
            continue
        unit = _unit_price(cur)
        if unit is None:
            continue
        out[days[i]] = (cur["ccp"] - ccp_prev) * unit
    return out


# --------------------------------------------------------------------------- #
# Write-path (background, prod). Best-effort: nunca propaga al loop.
# --------------------------------------------------------------------------- #
def fetch_ard_fci_rows(max_workers: int = 5) -> List[dict]:
    """Trae las 5 categorías de ArgentinaDatos FCI en paralelo → filas normalizadas
    `{fondo, vcp, ccp, patrimonio, fecha}`. Best-effort por categoría (las que fallan
    se saltan)."""
    rows: List[dict] = []

    def _one(item):
        tipo, url = item
        try:
            resp = httpx.get(url, timeout=15.0, headers={"User-Agent": "monitor-renta-fija/1.0"})
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001 — best-effort por categoría
            logger.warning("fci_history: ARD %s failed (%s)", tipo, e)
            return []
        out = []
        for r in data or ():
            if not isinstance(r, dict):
                continue
            out.append({"fondo": r.get("fondo"), "vcp": _f(r.get("vcp")),
                        "ccp": _f(r.get("ccp")), "patrimonio": _f(r.get("patrimonio")),
                        "fecha": r.get("fecha")})
        return out

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for partial in ex.map(_one, list(_ARD_FCI.items())):
            rows.extend(partial)
    return rows


def record_from_ard(store: "FCIHistoryStore", on_date: date) -> int:
    """Fetchea el corte de hoy de ArgentinaDatos y lo acumula en el store. Devuelve
    cuántas filas (fondos con ccp) se persistieron. Best-effort: si ARD está caído,
    devuelve 0 sin romper el loop."""
    return store.record_snapshot(fetch_ard_fci_rows(), on_date)


# --------------------------------------------------------------------------- #
# Singleton de proceso (read-path del panel + write-path de la task comparten el
# store/cache). Path desde settings (override por env MONITOR_FCI_HISTORY_DB en tests).
# --------------------------------------------------------------------------- #
_STORE: Optional[FCIHistoryStore] = None
_STORE_LOCK = threading.Lock()


def get_fci_history_store() -> FCIHistoryStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = FCIHistoryStore(settings.fci_history_db)
    return _STORE
