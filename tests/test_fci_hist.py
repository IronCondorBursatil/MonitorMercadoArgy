"""core/domain/fci/hist.py — reconstrucción de cuotaparte (anclada a retornos reales) + merge real."""

from datetime import date, timedelta

import pytest

from core.domain.fci import HIST_N
from core.domain.fci.hist import merge_real_hist, reconstruct_hist


def _rend(directo_12m=30.0, directo_1m=2.0):
    r = {p: {"tna": None, "directo": None} for p in
         ("dias_7", "mes_1", "dias_90", "dias_180", "ytd", "meses_12")}
    r["mes_1"]["directo"] = directo_1m
    r["meses_12"]["directo"] = directo_12m
    return r


def test_reconstruct_len_and_base100():
    h = reconstruct_hist(100.0, _rend(), "k1")
    assert len(h) == HIST_N
    assert h[0] == pytest.approx(100.0, abs=0.01)


def test_reconstruct_passes_through_12m_anchor():
    # end/start ≈ 1 + retorno 12m real (la curva pasa por el ancla de 12m)
    h = reconstruct_hist(100.0, _rend(directo_12m=30.0), "k2")
    assert h[-1] / h[0] == pytest.approx(1.30, rel=0.01)


def test_reconstruct_none_without_vcp_or_anchors():
    assert reconstruct_hist(None, _rend(), "k") is None
    empty = {p: {"tna": None, "directo": None} for p in
             ("dias_7", "mes_1", "dias_90", "dias_180", "ytd", "meses_12")}
    assert reconstruct_hist(100.0, empty, "k") is None    # <2 anclas


def test_merge_real_hist_uses_real_when_covered():
    today = date(2026, 6, 4)
    axis = [(today - timedelta(days=d)).isoformat() for d in (40, 30, 20, 10, 0)]
    axis = sorted(axis)                                   # oldest→hoy
    real = {date.fromisoformat(axis[0]): {"vcp": 100.0},
            date.fromisoformat(axis[-1]): {"vcp": 110.0}}
    rec = [1.0] * len(axis)
    series, real_flag = merge_real_hist(rec, real, axis)
    assert real_flag is True
    assert series[0] == pytest.approx(100.0)             # base-100
    assert series[-1] == pytest.approx(110.0)            # creció 10% real


def test_merge_real_hist_falls_back_when_sparse():
    today = date(2026, 6, 4)
    axis = [(today - timedelta(days=d)).isoformat() for d in (40, 30, 20, 10, 0)]
    axis = sorted(axis)
    real = {today: {"vcp": 110.0}}                        # 1 solo punto reciente
    rec = [100.0, 101.0, 102.0, 103.0, 104.0]
    series, real_flag = merge_real_hist(rec, real, axis)
    assert real_flag is False and series == rec
