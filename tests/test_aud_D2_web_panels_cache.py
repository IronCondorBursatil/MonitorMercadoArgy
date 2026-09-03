"""Auditoría D2 — `_CI_METRICS_CACHE` se mutaba sin sincronización.

`panel_rows` es un handler SYNC: FastAPI lo corre en el threadpool de anyio. Los dos
paneles con selector CI (`SETTLE_FILTER_PANELS = {"bonares", "cer"}`) disparan su
`hx-get` con el MISMO evento `sse:refresh`, así que dos hilos entran a `_ci_metrics`
casi simultáneamente en cada ciclo. El cache era un dict de módulo con tres ventanas
de read-modify-write no atómico (purga doble → KeyError, iteración mientras el otro
borra → RuntimeError, y check-then-get a caballo de un bump de revisión).

El test mete un dict instrumentado que, mientras UN hilo recorre el cache para
purgarlo, deja correr al otro: si la sección crítica no está protegida, el segundo
hilo borra las mismas claves y el primero revienta.
"""

from __future__ import annotations

import threading

import pytest

from apps.web.routers import panels


class _FakeReport:
    """Stub del motor: `_ci_metrics` sólo necesita que devuelva algo."""

    def __init__(self, *_a, **_kw):
        pass

    def execute(self, types, **_kw):
        return [("metrics", tuple(sorted(types)))]


class _FakeState:
    hub = object()


class _FakeApp:
    state = _FakeState()


class _FakeRequest:
    app = _FakeApp()


class _GatedCache(dict):
    """Dict que, la primera vez que lo recorren, cede el paso al otro hilo.

    `wait()` devuelve True sólo si el otro hilo consiguió terminar su propia pasada
    por el cache mientras nosotros lo estábamos recorriendo — es decir, si los dos
    estuvieron dentro de la sección crítica a la vez."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.armed = True
        self.other_finished = threading.Event()
        self.entered = threading.Event()
        self.interleaved = False

    def __iter__(self):
        keys = list(dict.keys(self))
        if self.armed:
            self.armed = False
            self.entered.set()
            self.interleaved = self.other_finished.wait(0.4)
        return iter(keys)


@pytest.fixture
def _stub_engine(monkeypatch):
    monkeypatch.setattr(panels, "GenerateMonitorReport", _FakeReport)
    monkeypatch.setattr(panels, "HubMarketDataProvider",
                        lambda *_a, **_kw: object())
    monkeypatch.setattr(panels, "get_repo", lambda: object())


def test_dos_paneles_ci_concurrentes_no_se_pisan_el_cache(_stub_engine, monkeypatch):
    cache = _GatedCache({(0, "bonares"): ["viejo"], (0, "cer"): ["viejo"]})
    monkeypatch.setattr(panels, "_CI_METRICS_CACHE", cache)
    req = _FakeRequest()
    boom: list[BaseException] = []

    def _worker():
        try:
            panels._ci_metrics("bonares", req, None, revision=1)
        except BaseException as e:     # noqa: BLE001 — lo reportamos como fallo
            boom.append(e)

    t = threading.Thread(target=_worker)
    t.start()
    assert cache.entered.wait(2), "el hilo A nunca llegó a recorrer el cache"
    # Hilo B (el otro panel CI del mismo ciclo de refresh) entra ahora.
    panels._ci_metrics("cer", req, None, revision=1)
    cache.other_finished.set()
    t.join(5)

    assert not boom, f"la purga concurrente reventó: {boom[0]!r}"
    assert not cache.interleaved, \
        "dos hilos dentro de la sección crítica del cache a la vez (falta lock)"
    assert set(cache) == {(1, "bonares"), (1, "cer")}, dict(cache)


def test_hit_de_cache_no_reejecuta_el_motor(_stub_engine, monkeypatch):
    """El memo sigue funcionando (y un resultado VACÍO también cuenta como hit)."""
    monkeypatch.setattr(panels, "_CI_METRICS_CACHE", {(7, "cer"): []})
    calls = {"n": 0}

    class _Counting(_FakeReport):
        def execute(self, types, **kw):
            calls["n"] += 1
            return super().execute(types, **kw)

    monkeypatch.setattr(panels, "GenerateMonitorReport", _Counting)
    out = panels._ci_metrics("cer", _FakeRequest(), None, revision=7)
    assert out == []
    assert calls["n"] == 0, "un resultado cacheado vacío volvió a correr el motor"
