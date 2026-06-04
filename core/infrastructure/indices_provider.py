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
from datetime import date, datetime, timedelta
from typing import Dict, Optional

from config.settings import DATA_DIR
from core.infrastructure._http import http_get_json

logger = logging.getLogger(__name__)


_BCRA_BASE = "https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias"

_HISTORY_DIR = os.path.join(DATA_DIR, "history")
_CER_CSV      = os.path.join(_HISTORY_DIR, "cer_diario.csv")
_TAMAR_CSV    = os.path.join(_HISTORY_DIR, "tamar_diario.csv")
_A3500_CSV    = os.path.join(_HISTORY_DIR, "a3500_diario.csv")
_RESERVAS_CSV = os.path.join(_HISTORY_DIR, "reservas_diario.csv")


def _fetch_series(variable_id: int, days: int) -> Dict[date, float]:
    """Pull last `days` days of a BCRA monetary variable."""
    end = date.today()
    start = end - timedelta(days=days)
    url = f"{_BCRA_BASE}/{variable_id}?Desde={start}&Hasta={end}"
    out: Dict[date, float] = {}
    try:
        payload = http_get_json(url, timeout=10, user_agent="Mozilla/5.0",
                                source=f"BCRA/var{variable_id}")
        results = payload.get("results", [])
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


def _load_csv(path: str) -> Dict[date, float]:
    """Read a `fecha,valor` CSV into a date->float dict. Missing file = {}."""
    out: Dict[date, float] = {}
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

    _CER_FETCH_DAYS = 30
    _TAMAR_BOOTSTRAP_DAYS = 3 * 365
    _TAMAR_TOPUP_DAYS = 30
    _A3500_FETCH_DAYS = 30
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

    def _fetch_all(self):
        with self._lock:
            if not self._disk_loaded:
                self._hydrate_from_disk()
            if self._last_attempt == date.today():
                return
            type(self)._last_attempt = date.today()

            cer_new = _fetch_series(30, days=self._CER_FETCH_DAYS)
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

            a3500_new = _fetch_series(5, days=self._A3500_FETCH_DAYS)
            if a3500_new:
                added = len(set(a3500_new) - set(self._cache_a3500))
                self._cache_a3500.update(a3500_new)
                _save_csv(_A3500_CSV, self._cache_a3500)
                logger.info(f"A3500: +{added} new points, {len(self._cache_a3500)} total.")

            # Variable 1 = Reservas Internacionales del BCRA (USD millones)
            reservas_new = _fetch_series(1, days=self._RESERVAS_FETCH_DAYS)
            if reservas_new:
                added = len(set(reservas_new) - set(self._cache_reservas))
                self._cache_reservas.update(reservas_new)
                _save_csv(_RESERVAS_CSV, self._cache_reservas)
                logger.info(f"Reservas: +{added} new points, {len(self._cache_reservas)} total.")

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
