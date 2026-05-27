import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import List, Optional

from core.domain.interfaces import IInstrumentsRepository, IMarketDataProvider
from core.domain.models import Instrument, InstrumentMetrics, MarketSnapshot
from core.domain.services import FinancialEngine
from core.infrastructure.fx_provider import DolarAPIProvider
from core.infrastructure.indices_provider import BCRAIndicesProvider

logger = logging.getLogger(__name__)


class GenerateMonitorReport:
    def __init__(self,
                 instruments_repo: IInstrumentsRepository,
                 market_provider: IMarketDataProvider):
        self.repo = instruments_repo
        self.provider = market_provider

    def execute(self, instrument_types: List[str]) -> List[InstrumentMetrics]:
        indices = BCRAIndicesProvider(excel_repo=self.repo)
        fx = DolarAPIProvider()

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

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = []
            for inst in all_instruments:
                snapshot = snapshots_dict.get(inst.ticker)
                if snapshot is None:
                    continue
                snapshot.instrument = inst
                futures.append(executor.submit(
                    self._enrich_metrics, inst, snapshot, indices, fx, mep_offer, cable_offer))

            results = [f.result() for f in futures]

        return [r for r in results if r is not None]

    @staticmethod
    def _sovereign_ars_usd_price(inst: Instrument, snapshot: MarketSnapshot,
                                 mep_offer, cable_offer) -> Optional[float]:
        """Para la pata ARS de un soberano (BONAR/GLOBAL sin sufijo D/C, que cotiza
        en pesos) devuelve el precio USD implícito = pesos ÷ offer (MEP si BONAR,
        CABLE si GLOBAL). None si no aplica o falta el dólar — entonces se usa el
        precio tal cual (las métricas en ese caso quedan como antes)."""
        if not snapshot.price or inst.instrument_type not in ("BONAR", "GLOBAL"):
            return None
        if (inst.ticker or "").upper().endswith(("D", "C")):
            return None  # patas MEP/CABLE ya cotizan en USD
        div = mep_offer if inst.instrument_type == "BONAR" else cable_offer
        if not div or div <= 0:
            return None
        return snapshot.price / div

    def _enrich_metrics(self, inst: Instrument, snapshot: MarketSnapshot, indices, fx,
                        mep_offer=None, cable_offer=None) -> Optional[InstrumentMetrics]:
        # Per-instrument failures must not abort the whole batch — one bad
        # cashflow row would silently take down every panel under the
        # consolidated execute(). Return None on failure; execute() already
        # filters Nones out of the result list.
        try:
            hist = self.provider.fetch_historical_prices(inst.ticker, 365)

            today = date.today()
            px_7d = hist.get(today - timedelta(days=7))
            px_30d = hist.get(today - timedelta(days=30))
            px_1y = hist.get(today - timedelta(days=365))

            # Pricing snapshot: para la pata ARS de un soberano usamos el precio
            # USD implícito (pesos ÷ MEP/CABLE) → TIR/V.Téc/MD/paridad correctas.
            # `snapshot` (precio en pesos) se conserva para el display del panel.
            pricing_snap = snapshot
            usd_px = self._sovereign_ars_usd_price(inst, snapshot, mep_offer, cable_offer)
            if usd_px is not None:
                pricing_snap = snapshot.model_copy(update={"price": usd_px})

            metrics = InstrumentMetrics(snapshot=snapshot)
            metrics.tir = FinancialEngine.calculate_tir(pricing_snap, indices_provider=indices, fx_provider=fx)
            metrics.technical_value = FinancialEngine.calculate_technical_value(
                pricing_snap, indices_provider=indices, fx_provider=fx,
            )

            if metrics.technical_value and pricing_snap.price:
                metrics.parity = pricing_snap.price / metrics.technical_value

            if metrics.tir is not None:
                metrics.duration = FinancialEngine.calculate_duration(pricing_snap, metrics.tir)

            metrics.variance_7d = FinancialEngine.calculate_pct_change(snapshot.price, px_7d)
            metrics.variance_30d = FinancialEngine.calculate_pct_change(snapshot.price, px_30d)
            metrics.variance_1y = FinancialEngine.calculate_pct_change(snapshot.price, px_1y)

            return metrics
        except Exception as e:
            logger.warning(f"Enrich failed for {inst.ticker}: {type(e).__name__}: {e}")
            return None
