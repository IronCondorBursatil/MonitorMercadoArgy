"""core/domain/fci/lens.py — lente de moneda (devaluación A3500 + inflación CER por período)."""

from datetime import date

import pytest

from core.domain.fci.lens import _pct_change, compute_macro


def test_pct_change_in_coverage():
    s = {date(2026, 1, 1): 100.0, date(2026, 6, 1): 110.0}
    # target = base-30 = 2026-05-02, asof=100; base asof=110 → +10%
    assert _pct_change(s, date(2026, 6, 1), 30) == pytest.approx(10.0)


def test_pct_change_out_of_coverage_is_none():
    s = {date(2026, 1, 1): 100.0, date(2026, 6, 1): 110.0}
    # 365 días antes (2025-06-01) precede a la serie → None (no inventa 0)
    assert _pct_change(s, date(2026, 6, 1), 365) is None
    assert _pct_change({}, date(2026, 6, 1), 30) is None


def test_compute_macro_shape_and_real_values():
    cer = {date(2026, 1, 1): 700.0, date(2026, 6, 4): 784.0}
    a3500 = {date(2026, 1, 1): 1180.0, date(2026, 6, 4): 1438.0}
    macro = compute_macro(cer, a3500, mep_now=1255.0, fecha_base="2026-06-04")
    assert set(macro) == {"mep", "cer", "mep_now"}
    assert macro["mep_now"] == 1255.0
    # ambos bloques tienen los 6 períodos
    for blk in ("mep", "cer"):
        assert set(macro[blk]) == {"dias_7", "mes_1", "dias_90", "dias_180", "ytd", "meses_12"}
    # 12m fuera de cobertura (serie arranca 2026-01-01) → None
    assert macro["cer"]["meses_12"] is None
    # ytd con cobertura → valor real ~12%
    assert macro["cer"]["ytd"] == pytest.approx((784.0 / 700.0 - 1) * 100, rel=0.01)


def test_compute_macro_mep_now_guard():
    assert compute_macro({}, {}, mep_now=0, fecha_base="2026-06-04")["mep_now"] is None
    assert compute_macro({}, {}, mep_now=None, fecha_base=None)["mep_now"] is None
