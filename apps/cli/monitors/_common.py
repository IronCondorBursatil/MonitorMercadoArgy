"""Bootstrap for the BEI computation: cached Excel repo + use-case factory."""
from config.settings import MASTER_XLSX
from core.infrastructure.repositories import (
    Data912MarketDataProvider,
    ExcelInstrumentsRepository,
)
from core.use_cases.generate_report import GenerateMonitorReport

_REPO_CACHE: dict[str, ExcelInstrumentsRepository] = {}


def get_repository(path: str = MASTER_XLSX) -> ExcelInstrumentsRepository:
    if path not in _REPO_CACHE:
        _REPO_CACHE[path] = ExcelInstrumentsRepository(path)
    return _REPO_CACHE[path]


def build_use_case() -> GenerateMonitorReport:
    return GenerateMonitorReport(get_repository(), Data912MarketDataProvider())
