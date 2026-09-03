"""Auditoría lote B — el fallback XBUE del calendario debe pedirse por RANGO y cachearse.

Fuera de la cobertura del Excel de feriados (2020-2029) `_es_habil` caía a
`_xbue_habil`, que pegaba una llamada a pandas_market_calendars POR FECHA (~2.6 ms).
`bond_detail._build_anchors` recorre día por día hasta el vto + 40 d y `cer_projection`
lo llama 2× por request → CUAP (2045) tardaba ~30 s por apertura del cajón
"Proyección CER", clavando un worker del threadpool.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pytest

from core import holiday_engine as he


@pytest.fixture(autouse=True)
def _clear_cache():
    he._xbue_ruedas_del_anio.cache_clear()
    yield
    he._xbue_ruedas_del_anio.cache_clear()


def _fuera_de_cobertura(year: int) -> bool:
    lo, hi = he._ar_cobertura()
    return not (lo <= year <= hi)


def test_una_sola_consulta_por_anio_fuera_de_cobertura(monkeypatch):
    calls = []
    real = he._get_byma()

    class _Counting:
        def schedule(self, start, end):
            calls.append((start, end))
            return real.schedule(start, end)

    monkeypatch.setattr(he, "_get_byma", lambda: _Counting())
    he._xbue_ruedas_del_anio.cache_clear()

    assert _fuera_de_cobertura(2045), "el test asume que 2045 está fuera de cobertura"
    d = date(2045, 3, 15)
    for _ in range(50):
        he._es_habil(d)
    assert len(calls) == 1, f"pegó {len(calls)} veces al calendario: {calls}"

    # un año entero de fechas sigue siendo UNA sola consulta (schedule por rango)
    for i in range(365):
        he._es_habil(date(2045, 1, 1) + timedelta(days=i))
    assert len(calls) == 1, f"pegó {len(calls)} veces al calendario: {calls}"


def test_el_resultado_por_rango_coincide_con_la_consulta_por_fecha():
    """El cambio es de performance, no de calendario: mismo veredicto que el
    schedule fecha-por-fecha que hacía antes."""
    byma = he._get_byma()
    d = date(2045, 1, 1)
    while d < date(2045, 3, 1):
        esperado = not byma.schedule(d.strftime("%Y-%m-%d"), d.strftime("%Y-%m-%d")).empty
        assert he._xbue_habil(d) is esperado, d
        d += timedelta(days=1)


def test_horizonte_largo_fuera_de_cobertura_no_tarda_segundos():
    """Barrido tipo `_build_anchors` de CUAP (hoy → 2045). Antes: ~15 s por
    pasada; ahora ~1 s la primera y ~0 la segunda."""
    base = date(2026, 9, 3)
    n = (date(2045, 12, 31) - base).days
    t0 = time.perf_counter()
    for i in range(n):
        he._es_habil(base + timedelta(days=i))
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    for i in range(n):
        he._es_habil(base + timedelta(days=i))
    warm = time.perf_counter() - t0
    assert cold < 5.0, f"primera pasada {cold:.2f}s (antes ~15 s)"
    assert warm < 0.5, f"segunda pasada {warm:.2f}s — el cache no está pegando"
