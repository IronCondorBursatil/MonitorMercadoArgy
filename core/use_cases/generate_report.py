import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from core.domain.interfaces import IInstrumentsRepository, IMarketDataProvider
from core.domain.models import Instrument, InstrumentMetrics, MarketSnapshot
from core.domain.services import FinancialEngine
from core.infrastructure.fx_provider import DolarAPIProvider
from core.infrastructure.indices_provider import BCRAIndicesProvider
from config.settings import settings

logger = logging.getLogger(__name__)

# Cache de bases históricas (Sem/1M/3M/YTD/1A) por (ticker, day).
# Las fechas de referencia (7/30/90d atrás, inicio de año, 1y atrás) no cambian
# dentro del día, así que basta invalidar al rollover de fecha.
_HIST_BASE_CACHE: Dict[Tuple[str, date], Tuple] = {}
_HIST_BASE_CACHE_DAY: Optional[date] = None
_HIST_BASE_LOCK = threading.Lock()


def _asof_price(series: dict, target: date, tol_days: Optional[int] = None) -> Optional[float]:
    """Precio del último día con dato EN o ANTES de `target`. El histórico solo
    tiene ruedas (sin fines de semana/feriados), así que un match exacto por fecha
    devolvía None la mayoría de las veces; buscar el último ≤ target lo hace robusto
    a los huecos del calendario.

    `tol_days` (opcional): si el dato encontrado está MÁS de `tol_days` antes del
    objetivo, se descarta (None). Evita el número engañoso de una fuente desfasada
    — p.ej. una ventana de "7 días" cayendo a un cierre de hace un mes (que hacía
    que "Sem" y "1M" dieran idénticos). Con dato fresco nunca se gatilla."""
    if not series:
        return None
    best = max((d for d in series if d <= target), default=None)
    if best is None:
        return None
    if tol_days is not None and (target - best).days > tol_days:
        return None
    return series[best]


def _hist_bases(ticker: str, today: date, provider) -> Tuple:
    """Precios de referencia históricos (Sem/1M/3M/YTD/1A) para `ticker` en `today`.

    Memoizado por (ticker, today): dentro del mismo día el dato no cambia (los
    cierres de semanas/meses atrás son inmutables). El cache se limpia al rollover
    de fecha para evitar que las referencias del año nuevo apunten al año anterior."""
    global _HIST_BASE_CACHE_DAY
    cache_key = (ticker, today)
    with _HIST_BASE_LOCK:
        if _HIST_BASE_CACHE_DAY != today:
            _HIST_BASE_CACHE.clear()
            _HIST_BASE_CACHE_DAY = today
        if cache_key in _HIST_BASE_CACHE:
            return _HIST_BASE_CACHE[cache_key]

    # Compute outside the lock — provider call is safe to run concurrently.
    # days=400 acota a ~13 meses (cubre 1A=365d + la tolerancia de 12d).
    hist = provider.fetch_historical_prices(ticker, 400)
    # tol_days por ventana: tolera el hueco de calendario (findes/feriados)
    # pero descarta bases demasiado viejas (fuente desfasada → engañoso).
    px_7d = _asof_price(hist, today - timedelta(days=7), tol_days=5)
    px_30d = _asof_price(hist, today - timedelta(days=30), tol_days=8)
    px_90d = _asof_price(hist, today - timedelta(days=90), tol_days=12)
    # YTD: cierre del año anterior (último día ≤ 31-dic) como base.
    px_ytd = _asof_price(hist, date(today.year, 1, 1) - timedelta(days=1), tol_days=12)
    px_1y = _asof_price(hist, today - timedelta(days=365), tol_days=12)
    result = (px_7d, px_30d, px_90d, px_ytd, px_1y)

    with _HIST_BASE_LOCK:
        _HIST_BASE_CACHE[cache_key] = result
    return result


class GenerateMonitorReport:
    def __init__(self,
                 instruments_repo: IInstrumentsRepository,
                 market_provider: IMarketDataProvider,
                 indices: Optional[object] = None,
                 fx: Optional[object] = None):
        self.repo = instruments_repo
        self.provider = market_provider
        # Los singletons de app.state se inyectan aquí para evitar reinstanciar
        # en cada execute() — los call-sites de app.py los pasan; tests usan mocks.
        # Si no se inyectan, se crean con el mismo default que antes (retro-compat).
        self._indices = indices
        self._fx = fx

    def execute(self, instrument_types: List[str],
                settle_date: Optional[date] = None,
                settle_lag: int = 1) -> List[InstrumentMetrics]:
        """Fija la liquidación del plazo elegido EN CONSONANCIA:
          - `settle_date`: fecha de descuento de TIR/MD (None → T+1; CI → date.today()).
          - `settle_lag`: plazo BYMA para la indexación CER de V.Téc (1=24hs, 0=CI).
        Default (None, 1) = convención 24hs actual (sin cambios)."""
        indices = self._indices if self._indices is not None else BCRAIndicesProvider(excel_repo=self.repo)
        fx = self._fx if self._fx is not None else DolarAPIProvider()

        # Offers (venta) para convertir la pata ARS de soberanos a su USD implícito:
        # BONAR (ley arg) ÷ MEP, GLOBAL (ley NY) ÷ CABLE. dolarapi: bolsa=MEP,
        # contadoconliqui=CABLE. Se leen 1× (cache class-level), no por instrumento.
        mep_q = fx.get_quote("bolsa")
        ccl_q = fx.get_quote("contadoconliqui")
        mep_offer = (mep_q or {}).get("venta")
        cable_offer = (ccl_q or {}).get("venta")

        all_instruments: List[Instrument] = []
        for t in instrument_types:
            all_instruments.extend(self.repo.get_instruments_by_type(t))

        if not all_instruments:
            logger.warning(f"No instruments found for types: {instrument_types}")
            return []

        tickers = [i.ticker for i in all_instruments]
        snapshots_dict = self.provider.fetch_snapshots(tickers)

        with ThreadPoolExecutor(max_workers=settings.engine_workers) as executor:
            futures = []
            for inst in all_instruments:
                snapshot = snapshots_dict.get(inst.ticker)
                if snapshot is None:
                    continue
                snapshot.instrument = inst
                futures.append(executor.submit(
                    self._enrich_metrics, inst, snapshot, indices, fx, mep_offer,
                    cable_offer, settle_date, settle_lag))

            results = [f.result() for f in futures]

        return [r for r in results if r is not None]

    @staticmethod
    def _sovereign_ars_usd_price(inst: Instrument, snapshot: MarketSnapshot,
                                 mep_offer, cable_offer) -> Optional[float]:
        """Para la pata PESOS de un soberano hard-dollar (BONAR/GLOBAL/BOPREAL que
        cotiza en pesos) devuelve el precio USD implícito = pesos ÷ offer. GLOBAL
        (ley NY) usa CABLE; BONAR y BOPREAL (ley local) usan MEP. None si no aplica o
        falta el dólar — entonces se usa el precio tal cual.

        La pata pesos se detecta por NO terminar en D/C (MEP/CABLE ya cotizan en USD):
        BONAR/GLOBAL pesos = sin sufijo (AL30); BOPREAL pesos = base BPO* (BPOC7)."""
        if not snapshot.price or inst.instrument_type not in ("BONAR", "GLOBAL", "BOPREAL"):
            return None
        if (inst.ticker or "").upper().endswith(("D", "C")):
            return None  # patas MEP/CABLE ya cotizan en USD
        div = cable_offer if inst.instrument_type == "GLOBAL" else mep_offer  # BONAR/BOPREAL → MEP
        if not div or div <= 0:
            return None
        return snapshot.price / div

    def _enrich_metrics(self, inst: Instrument, snapshot: MarketSnapshot, indices, fx,
                        mep_offer=None, cable_offer=None,
                        settle_date: Optional[date] = None,
                        settle_lag: int = 1) -> Optional[InstrumentMetrics]:
        # Per-instrument failures must not abort the whole batch — one bad
        # cashflow row would silently take down every panel under the
        # consolidated execute(). Return None on failure; execute() already
        # filters Nones out of the result list.
        try:
            today = date.today()
            bases = _hist_bases(inst.ticker, today, self.provider)

            # Pricing snapshot: para la pata ARS de un soberano usamos el precio
            # USD implícito (pesos ÷ MEP/CABLE) → TIR/V.Téc/MD/paridad correctas.
            # `snapshot` (precio en pesos) se conserva para el display del panel.
            pricing_snap = snapshot
            usd_px = self._sovereign_ars_usd_price(inst, snapshot, mep_offer, cable_offer)
            if usd_px is not None:
                pricing_snap = snapshot.model_copy(update={"price": usd_px})

            metrics = InstrumentMetrics(snapshot=snapshot)
            # settle_date: T+0 (CI) o T+1/default (24hs) → el descuento de TIR/MD usa
            # la MISMA liquidación que el precio. settle_lag mueve el ref CER de la V.Téc
            # al mismo plazo (ref de accrued = hoy en ambos, calibración Balanz intacta).
            metrics.tir = FinancialEngine.calculate_tir(
                pricing_snap, indices_provider=indices, fx_provider=fx, settle_date=settle_date)
            metrics.technical_value = FinancialEngine.calculate_technical_value(
                pricing_snap, indices_provider=indices, fx_provider=fx, settle_lag=settle_lag,
            )

            if metrics.technical_value and pricing_snap.price:
                metrics.parity = pricing_snap.price / metrics.technical_value

            if metrics.tir is not None:
                metrics.duration = FinancialEngine.calculate_duration(
                    pricing_snap, metrics.tir, settle_date=settle_date)

            px_7d, px_30d, px_90d, px_ytd, px_1y = bases
            metrics.variance_7d = FinancialEngine.calculate_pct_change(snapshot.price, px_7d)
            metrics.variance_30d = FinancialEngine.calculate_pct_change(snapshot.price, px_30d)
            metrics.variance_90d = FinancialEngine.calculate_pct_change(snapshot.price, px_90d)
            metrics.variance_ytd = FinancialEngine.calculate_pct_change(snapshot.price, px_ytd)
            metrics.variance_1y = FinancialEngine.calculate_pct_change(snapshot.price, px_1y)

            return metrics
        except Exception as e:
            logger.warning(f"Enrich failed for {inst.ticker}: {type(e).__name__}: {e}")
            return None
