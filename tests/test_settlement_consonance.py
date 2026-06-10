"""El plazo de liquidación (CI/T+0 vs 24hs/T+1) debe ir EN CONSONANCIA: el precio
del plazo y el descuento de TIR/MD usan la MISMA fecha de settlement.

- A nivel motor: `calculate_tir(settle_date=...)` cambia con la fecha de liquidación.
- A nivel use-case: `GenerateMonitorReport.execute(settle_date=...)` la propaga.
"""

from datetime import date, timedelta

from core.domain.models import Cashflow, Instrument, MarketSnapshot
from core.domain.services import FinancialEngine
from core.use_cases import generate_report as gr


def _bullet(ticker="TT"):
    mat = date.today() + timedelta(days=365)
    return Instrument(
        ticker=ticker, short_name="t", instrument_type="BONAR",
        maturity_date=mat, emission_date=date.today() - timedelta(days=365),
        cashflows=(Cashflow(date=mat, amortization=100.0, interest=5.0),),
    )


def test_tir_depends_on_settlement_date():
    snap = MarketSnapshot(instrument=_bullet(), price=95.0)
    tir_t0 = FinancialEngine.calculate_tir(snap, settle_date=date.today())
    tir_t30 = FinancialEngine.calculate_tir(snap, settle_date=date.today() + timedelta(days=30))
    assert tir_t0 is not None and tir_t30 is not None
    # Distinto horizonte de descuento → distinta TIR para el MISMO precio.
    assert abs(tir_t0 - tir_t30) > 1e-4


def test_generate_report_threads_settle_date(monkeypatch):
    class _Idx:
        def get_cer(self, *a, **k):
            return None

    class _Fx:
        def get_quote(self, k):
            return None

    # Evitar red: stubear los providers que execute() instancia internamente.
    monkeypatch.setattr(gr, "BCRAIndicesProvider", lambda **k: _Idx())
    monkeypatch.setattr(gr, "DolarAPIProvider", lambda: _Fx())

    inst = _bullet("TT")

    class _Repo:
        def get_instruments_by_type(self, t):
            return [inst] if t == "BONAR" else []

    class _Prov:
        def fetch_snapshots(self, tickers):
            return {"TT": MarketSnapshot(instrument=None, price=95.0)}

        def fetch_historical_prices(self, t, d):
            return {}

    m_t0 = gr.GenerateMonitorReport(_Repo(), _Prov()).execute(["BONAR"], settle_date=date.today())
    m_def = gr.GenerateMonitorReport(_Repo(), _Prov()).execute(["BONAR"])  # default T+1

    assert m_t0 and m_def and m_t0[0].tir is not None and m_def[0].tir is not None
    # CI (hoy) vs 24hs (T+1) → la TIR difiere (consonancia precio↔descuento).
    assert m_t0[0].tir != m_def[0].tir


def test_cer_vtec_index_date_shifts_with_settle_lag():
    """En bonos CER el plazo además mueve el ref de indexación de la V.Téc:
    settle_lag=0 (CI) usa el CER de T+0; settle_lag=1 (24hs) el de T+1."""
    from core.domain.conventions import cer_reference_date, settlement_byma_date

    mat = date.today() + timedelta(days=400)
    inst = Instrument(
        ticker="TX99", short_name="t", instrument_type="BONCER",
        maturity_date=mat, emission_date=date.today() - timedelta(days=100),
        cer_base=100.0, cer_lag=10,
        cashflows=(Cashflow(date=mat, amortization=100.0, interest=0.0),),
    )

    class _Idx:
        def __init__(self):
            self.calls = []

        def get_cer(self, d):
            self.calls.append(d)
            return float(d.toordinal())  # CER distinto por fecha pedida

    snap = MarketSnapshot(instrument=inst, price=100.0)
    idx = _Idx()
    v24 = FinancialEngine.calculate_technical_value(snap, idx, settle_lag=1)
    vci = FinancialEngine.calculate_technical_value(snap, idx, settle_lag=0)

    assert v24 != vci  # T+1 != T+0 → ref CER distinto → V.Téc distinta
    today = date.today()
    assert cer_reference_date(settlement_byma_date(today, 1), 10) in idx.calls
    assert cer_reference_date(settlement_byma_date(today, 0), 10) in idx.calls
