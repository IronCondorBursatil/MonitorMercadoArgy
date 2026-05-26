"""Equivalencia numérica Fase 1: el motor nuevo (Strategy + registry) debe dar
EXACTAMENTE los mismos valores que el `services.py` original para todos los
instrumentos del master y varios precios.

El motor original se extrae de git a `tests/_legacy_engine.py`. Se comparan los
métodos que NO usan `dataclasses.replace` en el legacy (calculate_tir / V.Téc /
duration / price_from_tir / theoretical_price / projected_payoff + métricas de
popup). Los wrappers que sí lo usan (recompute_*, tir_from_price) se cubren por
separado en el motor nuevo (no crashean y son consistentes con calculate_*).

Providers mock deterministas: aíslan la corrección del refactor de la data live.
"""

from datetime import date, timedelta

import pytest

from core.domain.models import MarketSnapshot
from core.domain.services import FinancialEngine as New

try:
    from tests._legacy_engine import FinancialEngine as Old
except Exception:  # pragma: no cover
    Old = None

from core.infrastructure.repositories import ExcelInstrumentsRepository
from config.settings import MASTER_XLSX


class MockIndices:
    """IndicesProvider determinista. CER monótono creciente; TAMAR constante."""
    def __init__(self):
        today = date.today()
        # Serie reciente para el fallback "future = última observada".
        self._cache_tamar = {today - timedelta(days=i): 30.0 for i in range(0, 6)}

    def get_cer(self, target=None):
        if target is None:
            return None
        return 1000.0 + (target - date(2020, 1, 1)).days * 0.5

    def get_tamar(self, target=None):
        return 30.0


class MockFx:
    def get_mayorista_venta(self):
        return 1100.0


@pytest.fixture(autouse=True)
def _clear_pricing_caches():
    """El avg TAMAR se cachea por (start,end,forecast) SIN identidad del provider.
    Otros tests (bond_detail, cartera) computan con índices reales y dejan el cache
    sucio → acá forzamos recomputo con MockIndices. Limpia motor nuevo + legacy."""
    from core.domain import conventions
    from core.domain.pricing import tamar
    tamar._AVG_TAMAR_CACHE.clear()
    tamar._AVG_TAMAR_DAY = None
    conventions._SETTLE_CACHE.clear()
    conventions._SETTLE_CACHE_DAY = None
    if Old is not None:
        import tests._legacy_engine as L
        L._AVG_TAMAR_CACHE.clear()
        L._AVG_TAMAR_DAY = None
        L._SETTLE_CACHE.clear()
        L._SETTLE_CACHE_DAY = None
    yield


def _close(a, b, tol=1e-7):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    # nan == nan (ambos no convergen)
    a_nan = isinstance(a, float) and a != a
    b_nan = isinstance(b, float) and b != b
    if a_nan or b_nan:
        return a_nan and b_nan
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


@pytest.fixture(scope="module")
def instruments():
    return ExcelInstrumentsRepository(MASTER_XLSX).get_all_instruments()


@pytest.mark.skipif(Old is None, reason="legacy engine not available")
def test_pricing_equivalence_all_instruments(instruments):
    idx, fx = MockIndices(), MockFx()
    settle = date.today() + timedelta(days=1)
    ref = date.today()
    mismatches = []

    for inst in instruments:
        for price in (95.0, 130.0, 158.2):
            snap = MarketSnapshot(instrument=inst, price=price, last_update=date.today())

            checks = {
                "tir": (
                    Old.calculate_tir(snap, idx, fx, settle_date=settle),
                    New.calculate_tir(snap, idx, fx, settle_date=settle),
                ),
                "vtec": (
                    Old.calculate_technical_value(snap, idx, fx, ref_date=ref),
                    New.calculate_technical_value(snap, idx, fx, ref_date=ref),
                ),
            }
            # duration: misma TIR para ambos (la nueva).
            tir_dur = checks["tir"][1]
            if tir_dur is not None and tir_dur == tir_dur:  # not nan
                checks["duration"] = (
                    Old.calculate_duration(snap, tir_dur, settle_date=settle),
                    New.calculate_duration(snap, tir_dur, settle_date=settle),
                )
            # price_from_tir: TIR fija.
            for tir_fix in (0.20, 0.45):
                checks[f"pft_{tir_fix}"] = (
                    Old.price_from_tir(snap, tir_fix, idx, fx, settle_date=settle),
                    New.price_from_tir(snap, tir_fix, idx, fx, settle_date=settle),
                )

            for name, (o, n) in checks.items():
                if not _close(o, n):
                    mismatches.append((inst.ticker, inst.instrument_type, name, price, o, n))

    assert not mismatches, f"{len(mismatches)} mismatches; first 15:\n" + "\n".join(
        f"  {t}/{ty} {nm} @px{px}: legacy={o!r} new={n!r}" for t, ty, nm, px, o, n in mismatches[:15]
    )


@pytest.mark.skipif(Old is None, reason="legacy engine not available")
def test_metrics_equivalence(instruments):
    idx = MockIndices()
    ref = date.today()
    mismatches = []
    for inst in instruments:
        pairs = {
            "accrued": (Old.accrued_interest(inst, ref), New.accrued_interest(inst, ref)),
            "residual": (Old.residual_nominal(inst, ref), New.residual_nominal(inst, ref)),
            "days_coupon": (Old.days_since_last_coupon(inst, ref), New.days_since_last_coupon(inst, ref)),
            "current_yield": (Old.current_yield(inst, 100.0, ref), New.current_yield(inst, 100.0, ref)),
            "dv01": (Old.dv01(inst, 0.3, ref), New.dv01(inst, 0.3, ref)),
            "convexity": (Old.convexity(inst, 0.3, ref), New.convexity(inst, 0.3, ref)),
            "vanilla_pv": (Old._vanilla_pv(inst, 0.3, ref), New._vanilla_pv(inst, 0.3, ref)),
            "theo_price": (
                Old.calculate_theoretical_price(inst, 0.3, ref),
                New.calculate_theoretical_price(inst, 0.3, ref),
            ),
            "projected_payoff": (
                Old.projected_payoff(inst, idx, ref_date=ref),
                New.projected_payoff(inst, idx, ref_date=ref),
            ),
        }
        for name, (o, n) in pairs.items():
            if not _close(o, n):
                mismatches.append((inst.ticker, inst.instrument_type, name, o, n))
    assert not mismatches, f"{len(mismatches)} mismatches; first 15:\n" + "\n".join(
        f"  {t}/{ty} {nm}: legacy={o!r} new={n!r}" for t, ty, nm, o, n in mismatches[:15]
    )


@pytest.mark.skipif(Old is None, reason="legacy engine not available")
def test_rate_conversions_equivalence():
    for tea in (None, -1.0, 0.0, 0.05, 0.25, 0.5, 1.0, 2.0):
        assert _close(Old.tea_to_tem(tea), New.tea_to_tem(tea))
        assert _close(Old.tea_to_tna(tea), New.tea_to_tna(tea))
        assert _close(Old.tea_to_tna_monthly(tea), New.tea_to_tna_monthly(tea))
        assert _close(Old.tea_to_tem_m12(tea), New.tea_to_tem_m12(tea))


def test_recompute_and_tir_from_price_smoke(instruments):
    """Los wrappers que el legacy no puede correr (usan dataclasses.replace) al
    menos no crashean en el motor nuevo y son consistentes con calculate_*."""
    idx, fx = MockIndices(), MockFx()
    settle = date.today() + timedelta(days=1)
    for inst in instruments:
        snap = MarketSnapshot(instrument=inst, price=120.0, last_update=date.today())
        # tir_from_price == calculate_tir con ese precio
        t1 = New.calculate_tir(snap, idx, fx, settle_date=settle)
        t2 = New.tir_from_price(snap, 120.0, idx, fx, settle_date=settle)
        assert _close(t1, t2)
        if inst.is_dual_tamar or inst.is_tamar_puro:
            New.recompute_as_tamar_puro(snap, idx)       # no crash
            New.recompute_as_tasa_fija(snap, idx)         # no crash
