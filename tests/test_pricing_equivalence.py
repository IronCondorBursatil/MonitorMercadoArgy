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
from config.settings import settings
from tests._clock import ref_date  # fecha fija anti-decaimiento (M0.1)


class MockIndices:
    """IndicesProvider determinista. CER monótono creciente; TAMAR constante."""
    def __init__(self):
        today = ref_date()
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


class _FrozenDate(date):
    """Subclase de date con today() fijo a ref_date(): congela el `date.today()`
    interno del motor LEGACY (que importa `from datetime import date`). Constructores
    y comparaciones se heredan intactos — solo cambia today()."""
    @classmethod
    def today(cls):
        rd = ref_date()
        return cls(rd.year, rd.month, rd.day)


@pytest.fixture(autouse=True)
def _frozen_clock_and_clean_caches(monkeypatch):
    """Anti-decaimiento COMPLETO (F1): congela el "hoy" de AMBOS motores a ref_date —
    el nuevo vía MONITOR_AS_OF (core/domain/clock.py: avg TAMAR, project_cer_at,
    síntesis de cashflows) y el legacy parcheando su símbolo `date` con _FrozenDate.
    Sin esto, el settle congelado (M0.1) no alcanzaba: la ventana de promedio TAMAR
    (`past_end = min(today, ...)`) y la extrapolación CER derivaban con el reloj real
    y los valores del test cambiaban con la fecha de corrida.

    Además limpia los caches de pricing: el avg TAMAR se cachea por
    (start,end,forecast) SIN identidad del provider — otros tests (bond_detail,
    cartera) computan con índices reales y dejarían el cache sucio. Al salir, el
    day-check de los caches (keyed por today) los auto-invalida para los módulos
    siguientes (vuelven al today real)."""
    monkeypatch.setenv("MONITOR_AS_OF", ref_date().isoformat())
    from core.domain import conventions
    from core.domain.pricing import tamar
    tamar._AVG_TAMAR_CACHE.clear()
    tamar._AVG_TAMAR_DAY = None
    conventions._SETTLE_CACHE.clear()
    conventions._SETTLE_CACHE_DAY = None
    if Old is not None:
        import tests._legacy_engine as L
        monkeypatch.setattr(L, "date", _FrozenDate)
        L._AVG_TAMAR_CACHE.clear()
        L._AVG_TAMAR_DAY = None
        L._SETTLE_CACHE.clear()
        L._SETTLE_CACHE_DAY = None
    yield


def test_both_engines_see_frozen_today():
    """Guard del congelamiento: si alguien des-congela un motor, esto falla antes
    de que la equivalencia se vuelva date-dependent en silencio."""
    from core.domain.clock import today as domain_today
    assert domain_today() == ref_date(), "motor nuevo: MONITOR_AS_OF no aplicado"
    if Old is not None:
        import tests._legacy_engine as L
        assert L.date.today() == ref_date(), "motor legacy: date.today() sin congelar"


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
    return ExcelInstrumentsRepository(str(settings.master_xlsx)).get_all_instruments()


@pytest.mark.skipif(Old is None, reason="legacy engine not available")
def test_pricing_equivalence_all_instruments(instruments):
    idx, fx = MockIndices(), MockFx()
    settle = ref_date() + timedelta(days=1)
    ref = ref_date()
    mismatches = []

    for inst in instruments:
        for price in (95.0, 130.0, 158.2):
            snap = MarketSnapshot(instrument=inst, price=price, last_update=ref_date())

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
    ref = ref_date()
    mismatches = []
    for inst in instruments:
        # `days_coupon` y `current_yield` se sacaron a propósito: son métricas de
        # DISPLAY (no invariantes de pricing) y se realinearon a la convención del
        # informe IAMC divergiendo del legacy congelado — days desde el día
        # hábil de pago, y current_yield = cupón anual / clean (ver
        # tests/test_yield_conventions.py). El harness sigue protegiendo TIR/PV/MD/
        # payoff/accrued/residual, que NO cambian.
        pairs = {
            "accrued": (Old.accrued_interest(inst, ref), New.accrued_interest(inst, ref)),
            "residual": (Old.residual_nominal(inst, ref), New.residual_nominal(inst, ref)),
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
    settle = ref_date() + timedelta(days=1)
    for inst in instruments:
        snap = MarketSnapshot(instrument=inst, price=120.0, last_update=ref_date())
        # tir_from_price == calculate_tir con ese precio
        t1 = New.calculate_tir(snap, idx, fx, settle_date=settle)
        t2 = New.tir_from_price(snap, 120.0, idx, fx, settle_date=settle)
        assert _close(t1, t2)
        if inst.is_dual_tamar or inst.is_tamar_puro:
            New.recompute_as_tamar_puro(snap, idx)       # no crash
            New.recompute_as_tasa_fija(snap, idx)         # no crash
