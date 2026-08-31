"""BCRA reference indices (CER, TAMAR, A3500, Reservas) with on-disk persistence.

Architectural exception: market data must come from Data912, but BCRA
publishes reference series only available from its own API.

Variables fetched (BCRA `Monetarias/{id}`):
  - 30: CER (Coeficiente de Estabilización de Referencia) — daily index level
  - 44: Tasa de interés TAMAR de bancos privados (TNA %)
  - 5:  Tipo de cambio mayorista de referencia (A3500) — fixing oficial EOD
        usado como spot DLR para TNA implícita de futuros fuera de rueda.
  - 1:  Reservas Internacionales del BCRA (USD millones) — widget principal.

Resilience: each series is mirrored to `data/history/{cer,tamar,a3500,reservas}_diario.csv`.
On startup the cache is hydrated from disk first, then BCRA is queried only
to top-up missing recent days. If BCRA is unreachable the project keeps
working with the last persisted snapshot — only data freshness degrades.
"""

import csv
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional

from core.infrastructure.history_paths import resolve_read, state_path

logger = logging.getLogger(__name__)

_AR_TZ = timezone(timedelta(hours=-3))


def _ar_today() -> date:
    """Fecha de hoy en hora Argentina (UTC-3, sin horario de verano).
    Reemplaza `date.today()` en los gates 1×/día para que el rollover ocurra
    a medianoche AR, no a medianoche UTC — el BCRA publica a las 18-20hs AR."""
    return datetime.now(tz=_AR_TZ).date()


_BCRA_BASE = "https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias"

# Semilla versionada vs. estado de runtime — ver core/infrastructure/history_paths.py.
# Estos CSV se reescriben en cada ciclo; si el destino cae en data/history/ (que esta
# en git) el `git pull` del deploy aborta contra el arbol sucio.
_CER_CSV      = state_path("cer_diario.csv")
_TAMAR_CSV    = state_path("tamar_diario.csv")
_A3500_CSV    = state_path("a3500_diario.csv")
_RESERVAS_CSV = state_path("reservas_diario.csv")


def _fetch_series(variable_id: int, days: int) -> Dict[date, float]:
    """Pull last `days` days of a BCRA monetary variable (sync fallback)."""
    import httpx
    end = _ar_today()
    start = end - timedelta(days=days)
    url = f"{_BCRA_BASE}/{variable_id}?Desde={start}&Hasta={end}"
    out: Dict[date, float] = {}
    try:
        resp = httpx.get(url, timeout=10.0, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results and "detalle" in results[0]:
            for item in results[0]["detalle"]:
                try:
                    d = datetime.strptime(item["fecha"], "%Y-%m-%d").date()
                    out[d] = float(item["valor"])
                except (KeyError, ValueError, TypeError):
                    continue
    except Exception as e:
        logger.warning(f"BCRA fetch failed for variable {variable_id}: {e}")
    return out


async def _async_fetch_series(client, variable_id: int, days: int) -> Dict[date, float]:
    """Pull last `days` days of a BCRA monetary variable (async)."""
    end = _ar_today()
    start = end - timedelta(days=days)
    url = f"{_BCRA_BASE}/{variable_id}?Desde={start}&Hasta={end}"
    out: Dict[date, float] = {}
    try:
        payload = await client.get_json(url, timeout=10.0, source=f"BCRA/var{variable_id}")
        results = payload.get("results", [])
        if results and "detalle" in results[0]:
            for item in results[0]["detalle"]:
                try:
                    d = datetime.strptime(item["fecha"], "%Y-%m-%d").date()
                    out[d] = float(item["valor"])
                except (KeyError, ValueError, TypeError):
                    continue
    except Exception as e:
        logger.warning(f"BCRA async fetch failed for variable {variable_id}: {e}")
    return out


def _load_csv(path: str) -> Dict[date, float]:
    """Read a `fecha,valor` CSV into a date->float dict. Missing file = {}.

    Si el archivo de ESTADO todavia no existe (clon nuevo, primer arranque tras el
    deploy), cae a la SEMILLA versionada del repo con el mismo nombre. La semilla
    solo se lee: la acumulacion posterior va siempre al state dir.
    """
    out: Dict[date, float] = {}
    path = resolve_read(path)
    if not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    d = datetime.strptime(row["fecha"], "%Y-%m-%d").date()
                    out[d] = float(row["valor"])
                except (KeyError, ValueError, TypeError):
                    continue
    except OSError as e:
        logger.warning(f"Could not read {path}: {e}")
    return out


def _save_csv(path: str, data: Dict[date, float]) -> None:
    """Persist date->float dict as `fecha,valor` CSV, sorted ascending.
    Writes to a tmp file then renames to avoid corruption on mid-write crash.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["fecha", "valor"])
            for d in sorted(data.keys()):
                writer.writerow([d.isoformat(), data[d]])
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"Could not persist {path}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


class BCRAIndicesProvider:
    """CER + TAMAR + A3500 + Reservas from BCRA, hydrated from disk on startup."""

    # CER y A3500 se bootstrappean ~400 días (el lente de moneda del panel FCI
    # necesita 3m/6m/ytd/12m de historia para deflactar por inflación/devaluación);
    # una vez cubierta la ventana, sólo se topup-ean los días recientes.
    _CER_BOOTSTRAP_DAYS = 400
    _CER_TOPUP_DAYS = 30
    _TAMAR_BOOTSTRAP_DAYS = 3 * 365
    _TAMAR_TOPUP_DAYS = 30
    _A3500_BOOTSTRAP_DAYS = 400
    _A3500_TOPUP_DAYS = 30
    # Reservas: 90 días para el chart histórico de la página Monitor BCRA.
    _RESERVAS_FETCH_DAYS = 90

    _lock = threading.Lock()
    _cache_cer:      Dict[date, float] = {}
    _cache_tamar:    Dict[date, float] = {}
    _cache_a3500:    Dict[date, float] = {}
    _cache_reservas: Dict[date, float] = {}
    _last_attempt: Optional[date] = None
    _disk_loaded: bool = False

    def __init__(self, excel_repo=None):
        self.excel_repo = excel_repo

    @classmethod
    def _hydrate_from_disk(cls):
        cls._cache_cer      = _load_csv(_CER_CSV)
        cls._cache_tamar    = _load_csv(_TAMAR_CSV)
        cls._cache_a3500    = _load_csv(_A3500_CSV)
        cls._cache_reservas = _load_csv(_RESERVAS_CSV)
        logger.info(
            "Loaded indices from disk: CER=%d, TAMAR=%d, A3500=%d, Reservas=%d points.",
            len(cls._cache_cer), len(cls._cache_tamar),
            len(cls._cache_a3500), len(cls._cache_reservas),
        )
        cls._disk_loaded = True

    @staticmethod
    def _fetch_window(cache: Dict[date, float], bootstrap_days: int, topup_days: int) -> int:
        """Días a pedir: `bootstrap_days` mientras la cache no cubra ~la ventana
        completa (vacía o con historia corta → backfill), luego `topup_days`."""
        if not cache:
            return bootstrap_days
        if (_ar_today() - min(cache)).days < bootstrap_days - 40:
            return bootstrap_days   # historia corta → backfillear la ventana
        return topup_days

    def _fetch_all(self):
        with self._lock:
            if not self._disk_loaded:
                self._hydrate_from_disk()
            if self._last_attempt == _ar_today():
                return
            type(self)._last_attempt = _ar_today()

            cer_days = self._fetch_window(self._cache_cer, self._CER_BOOTSTRAP_DAYS, self._CER_TOPUP_DAYS)
            cer_new = _fetch_series(30, days=cer_days)
            if cer_new:
                added = len(set(cer_new) - set(self._cache_cer))
                self._cache_cer.update(cer_new)
                _save_csv(_CER_CSV, self._cache_cer)
                logger.info(f"CER: +{added} new points, {len(self._cache_cer)} total.")

            tamar_days = self._TAMAR_TOPUP_DAYS if self._cache_tamar else self._TAMAR_BOOTSTRAP_DAYS
            tamar_new = _fetch_series(44, days=tamar_days)
            if tamar_new:
                added = len(set(tamar_new) - set(self._cache_tamar))
                self._cache_tamar.update(tamar_new)
                _save_csv(_TAMAR_CSV, self._cache_tamar)
                logger.info(f"TAMAR: +{added} new points, {len(self._cache_tamar)} total.")

            a3500_days = self._fetch_window(self._cache_a3500, self._A3500_BOOTSTRAP_DAYS, self._A3500_TOPUP_DAYS)
            a3500_new = _fetch_series(5, days=a3500_days)
            if a3500_new:
                added = len(set(a3500_new) - set(self._cache_a3500))
                self._cache_a3500.update(a3500_new)
                _save_csv(_A3500_CSV, self._cache_a3500)
                logger.info(f"A3500: +{added} new points, {len(self._cache_a3500)} total.")

            reservas_new = _fetch_series(1, days=self._RESERVAS_FETCH_DAYS)
            if reservas_new:
                added = len(set(reservas_new) - set(self._cache_reservas))
                self._cache_reservas.update(reservas_new)
                _save_csv(_RESERVAS_CSV, self._cache_reservas)
                logger.info(f"Reservas: +{added} new points, {len(self._cache_reservas)} total.")

    async def prefetch(self, client):
        """Precarga asincrónica para el refresh_loop (no bloquea hilos de CPU)."""
        with self._lock:
            if not self._disk_loaded:
                self._hydrate_from_disk()
            if self._last_attempt == _ar_today():
                return
            type(self)._last_attempt = _ar_today()

        import asyncio
        cer_days = self._fetch_window(self._cache_cer, self._CER_BOOTSTRAP_DAYS, self._CER_TOPUP_DAYS)
        tamar_days = self._TAMAR_TOPUP_DAYS if self._cache_tamar else self._TAMAR_BOOTSTRAP_DAYS
        a3500_days = self._fetch_window(self._cache_a3500, self._A3500_BOOTSTRAP_DAYS, self._A3500_TOPUP_DAYS)
        reservas_days = self._RESERVAS_FETCH_DAYS

        # Parallel fetch
        cer_f, tamar_f, a3500_f, res_f = await asyncio.gather(
            _async_fetch_series(client, 30, cer_days),
            _async_fetch_series(client, 44, tamar_days),
            _async_fetch_series(client, 5, a3500_days),
            _async_fetch_series(client, 1, reservas_days),
            return_exceptions=True
        )

        with self._lock:
            if isinstance(cer_f, dict) and cer_f:
                added = len(set(cer_f) - set(self._cache_cer))
                self._cache_cer.update(cer_f)
                _save_csv(_CER_CSV, self._cache_cer)
                if added: logger.info(f"CER: +{added} new points, {len(self._cache_cer)} total.")
            if isinstance(tamar_f, dict) and tamar_f:
                added = len(set(tamar_f) - set(self._cache_tamar))
                self._cache_tamar.update(tamar_f)
                _save_csv(_TAMAR_CSV, self._cache_tamar)
                if added: logger.info(f"TAMAR: +{added} new points, {len(self._cache_tamar)} total.")
            if isinstance(a3500_f, dict) and a3500_f:
                added = len(set(a3500_f) - set(self._cache_a3500))
                self._cache_a3500.update(a3500_f)
                _save_csv(_A3500_CSV, self._cache_a3500)
                if added: logger.info(f"A3500: +{added} new points, {len(self._cache_a3500)} total.")
            if isinstance(res_f, dict) and res_f:
                added = len(set(res_f) - set(self._cache_reservas))
                self._cache_reservas.update(res_f)
                _save_csv(_RESERVAS_CSV, self._cache_reservas)
                if added: logger.info(f"Reservas: +{added} new points, {len(self._cache_reservas)} total.")

    @staticmethod
    def _lookup(target: date, cache: Dict[date, float]) -> Optional[float]:
        """Return the value at `target`, falling back up to 14 calendar days.
        If still unfound, returns the most recent available value."""
        if not cache:
            return None
        if target in cache:
            return cache[target]
        for i in range(1, 15):
            prev = target - timedelta(days=i)
            if prev in cache:
                return cache[prev]
        return cache[max(cache.keys())]

    # ------------------------------------------------------------------ #
    # Public accessors
    # ------------------------------------------------------------------ #

    def get_cer(self, target_date: date) -> Optional[float]:
        self._fetch_all()
        return self._lookup(target_date, self._cache_cer)

    def cer_series(self) -> Dict[date, float]:
        """Serie CER {date: valor} (copia). Para el lente de moneda del panel FCI."""
        self._fetch_all()
        return dict(self._cache_cer)

    def a3500_series(self) -> Dict[date, float]:
        """Serie A3500 {date: valor} (copia). Proxy FX para el lente del panel FCI."""
        self._fetch_all()
        return dict(self._cache_a3500)

    def get_tamar(self, target_date: Optional[date] = None) -> Optional[float]:
        self._fetch_all()
        return self._lookup(target_date or date.today(), self._cache_tamar)

    def get_a3500(self, target_date: Optional[date] = None) -> Optional[float]:
        self._fetch_all()
        return self._lookup(target_date or date.today(), self._cache_a3500)

    def get_reservas_brutas(self, target_date: Optional[date] = None) -> Optional[float]:
        """Reservas Internacionales del BCRA (USD millones). Última publicada si no
        se especifica fecha (publicadas ~18-20hs hora AR del día hábil)."""
        self._fetch_all()
        return self._lookup(target_date or date.today(), self._cache_reservas)

    def get_reservas_delta(self) -> Optional[float]:
        """Diferencia en USD mm entre el último dato de reservas y el anterior.
        Proxy diario de la variación por intervención MULC + otros flujos."""
        self._fetch_all()
        if len(self._cache_reservas) < 2:
            return None
        sorted_dates = sorted(self._cache_reservas.keys(), reverse=True)
        return self._cache_reservas[sorted_dates[0]] - self._cache_reservas[sorted_dates[1]]

    def get_reservas_history(self, days: int = 90) -> list:
        """Lista [{fecha, valor}] de los últimos `days` días de reservas,
        ordenada ascendente. Para los charts de la página Monitor BCRA."""
        self._fetch_all()
        cutoff = date.today() - timedelta(days=days)
        return [
            {"fecha": d.isoformat(), "valor": v}
            for d, v in sorted(self._cache_reservas.items())
            if d >= cutoff
        ]

    def get_a3500_history(self, days: int = 90) -> list:
        """Lista [{fecha, valor}] de los últimos `days` días de A3500."""
        self._fetch_all()
        cutoff = date.today() - timedelta(days=days)
        return [
            {"fecha": d.isoformat(), "valor": v}
            for d, v in sorted(self._cache_a3500.items())
            if d >= cutoff
        ]

    def get_tamar_history(self, days: int = 90) -> list:
        """Lista [{fecha, valor}] de los últimos `days` días de TAMAR TNA."""
        self._fetch_all()
        cutoff = date.today() - timedelta(days=days)
        return [
            {"fecha": d.isoformat(), "valor": v}
            for d, v in sorted(self._cache_tamar.items())
            if d >= cutoff
        ]
