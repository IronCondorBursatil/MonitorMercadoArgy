from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from datetime import date
from core.domain.models import Instrument, MarketSnapshot


class IInstrumentsRepository(ABC):
    @abstractmethod
    def get_all_instruments(self) -> List[Instrument]:
        pass

    @abstractmethod
    def get_instruments_by_type(self, instrument_type: str) -> List[Instrument]:
        pass

    @abstractmethod
    def get_instrument_by_ticker(self, ticker: str) -> Optional[Instrument]:
        pass


class IMarketDataProvider(ABC):
    @abstractmethod
    def fetch_snapshots(self, tickers: List[str]) -> Dict[str, MarketSnapshot]:
        pass

    @abstractmethod
    def fetch_historical_prices(self, ticker: str, days: int) -> Dict[date, float]:
        pass
