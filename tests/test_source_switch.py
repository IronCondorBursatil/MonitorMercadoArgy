"""Hot-swap de la fuente del ProviderHub + registry make_source (sin red)."""

import asyncio

import httpx
import pytest

from core.infrastructure.async_http import ResilientClient
from core.infrastructure.byma.sources import (
    BymaOpenSource, BymaRealtimeError, BymaRealtimeSource, Data912Source, make_source,
)
from core.infrastructure.provider_hub import ProviderHub
from core.infrastructure.schemas import Data912Row


class _FakeSource:
    mode = "fake"
    label = "Fake"
    delayed = True

    async def fetch(self, client):
        return ({"24": {"AL30": Data912Row(symbol="AL30", c=99.0)},
                 "CI": {"AL30": Data912Row(symbol="AL30", c=98.0)}},
                {"AL30": "bonds"})


def test_hub_default_is_data912():
    c = ResilientClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    hub = ProviderHub(c)
    assert hub.active_mode == "data912"
    asyncio.run(c.aclose())


def test_hub_uses_active_source_and_swaps():
    async def run():
        c = ResilientClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
        hub = ProviderHub(c)
        try:
            hub.set_source(_FakeSource())
            snap = await hub.refresh_all()
            assert snap["AL30"].c == 99.0                # 24hs (default)
            assert hub.snapshot("CI")["AL30"].c == 98.0  # plazo CI
            assert hub.active_mode == "fake"
            assert hub.is_delayed is True
            assert hub.sources()["AL30"] == "bonds"
        finally:
            await c.aclose()
    asyncio.run(run())


def test_hub_provider_settle_param():
    """HubMarketDataProvider(settle=...) sirve el snapshot del plazo pedido."""
    class _Hist:
        def fetch_historical_prices(self, t, d):
            return {}

        def fetch_stock_history(self, t):
            return []

    async def run():
        from core.infrastructure.provider_hub import HubMarketDataProvider
        c = ResilientClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
        hub = ProviderHub(c)
        try:
            hub.set_source(_FakeSource())
            await hub.refresh_all()
            p24 = HubMarketDataProvider(hub, _Hist(), settle="24")
            pci = HubMarketDataProvider(hub, _Hist(), settle="CI")
            assert p24.fetch_snapshots(["AL30"])["AL30"].price == 99.0
            assert pci.fetch_snapshots(["AL30"])["AL30"].price == 98.0
        finally:
            await c.aclose()
    asyncio.run(run())


class _EmptySource:
    """Fuente activa que no trae nada (BYMA open pre-market: empty:true)."""
    mode = "byma_open"
    label = "BYMA open (20m)"
    delayed = True

    async def fetch(self, client):
        return ({"24": {}, "CI": {}}, {})


def _data912_handler(request):
    """MockTransport: Data912 sirve cierre previo; cualquier otro host, vacío."""
    if "data912.com" in str(request.url):
        return httpx.Response(200, json=[{"symbol": "AL30", "c": 70.5}])
    return httpx.Response(200, json=[])


def test_empty_active_source_backstops_to_data912():
    """Pre-market: la fuente activa (BYMA open) devuelve vacío y el snapshot está
    en blanco → el hub primero con el cierre previo de Data912 (board NO en 0.00).
    La fuente activa NO cambia (apenas BYMA abra, retoma)."""
    async def run():
        c = ResilientClient(transport=httpx.MockTransport(_data912_handler))
        hub = ProviderHub(c)
        try:
            hub.set_source(_EmptySource())
            snap = await hub.refresh_all()
            assert snap["AL30"].c == 70.5          # backstop pobló el board
            assert hub.snapshot("CI")["AL30"].c == 70.5
            assert hub.active_mode == "byma_open"  # la fuente activa NO se tocó
        finally:
            await c.aclose()
    asyncio.run(run())


def test_floor_does_not_clobber_live_price_on_transient_empty():
    """Floor Data912: un ciclo vacío transitorio de la activa NO pisa el precio vivo ya
    entregado — el floor solo rellena símbolos que la activa NO cubre (AL30 ya es de BYMA)."""
    def handler(request):
        if "data912.com" in str(request.url):
            return httpx.Response(200, json=[{"symbol": "AL30", "c": 70.5}])
        return httpx.Response(200, json=[])

    class _Flaky:
        mode = "byma_open"
        label = "BYMA open (20m)"
        delayed = True

        def __init__(self):
            self.n = 0

        async def fetch(self, client):
            self.n += 1
            if self.n == 1:  # 1er ciclo: BYMA entrega 99.0
                return ({"24": {"AL30": Data912Row(symbol="AL30", c=99.0)}, "CI": {}},
                        {"AL30": "bonds"})
            return ({"24": {}, "CI": {}}, {})  # 2do ciclo: vacío (transitorio)

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(handler))
        hub = ProviderHub(c)
        try:
            hub.set_source(_Flaky())
            await hub.refresh_all()         # BYMA 99.0 → AL30 lo cubre la activa
            snap = await hub.refresh_all()  # BYMA vacío → AL30 retiene 99.0 (no 70.5 del floor)
            assert snap["AL30"].c == 99.0
        finally:
            await c.aclose()
    asyncio.run(run())


def test_floor_fills_symbols_active_does_not_list():
    """BYMA lista solo AL30; Data912 (floor) trae AL30 y GD30 → el snapshot tiene ambos:
    AL30 de BYMA (la activa manda) y GD30 del floor Data912 (lo que BYMA no lista)."""
    def handler(request):
        if "data912.com" in str(request.url):
            return httpx.Response(200, json=[{"symbol": "AL30", "c": 70.5},
                                             {"symbol": "GD30", "c": 55.0}])
        return httpx.Response(200, json=[])

    class _Partial:
        mode = "byma_open"
        label = "BYMA open"
        delayed = True

        async def fetch(self, client):
            return ({"24": {"AL30": Data912Row(symbol="AL30", c=99.0)}, "CI": {}},
                    {"AL30": "bonds"})

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(handler))
        hub = ProviderHub(c)
        try:
            hub.set_source(_Partial())
            snap = await hub.refresh_all()
            assert snap["AL30"].c == 99.0    # la activa (BYMA) manda donde lista
            assert snap["GD30"].c == 55.0    # el floor Data912 rellena lo que BYMA no lista
        finally:
            await c.aclose()
    asyncio.run(run())


def test_floor_recovers_symbol_after_k_cycles_active_drops_it():
    """A3 — ventana deslizante K: un símbolo que la activa DEJA de listar lo retiene
    la activa durante K ciclos (anti-flicker) y recién al expirar la ventana el floor
    Data912 lo recupera. Distinto de un vacío transitorio (ese se cubre en otro test):
    acá la ausencia es persistente y debe ceder al floor tras K ausencias consecutivas."""
    def handler(request):
        if "data912.com" in str(request.url):
            return httpx.Response(200, json=[{"symbol": "AL30", "c": 70.5}])
        return httpx.Response(200, json=[])

    class _DropsAfterFirst:
        mode = "byma_open"
        label = "BYMA open"
        delayed = True

        def __init__(self):
            self.n = 0

        async def fetch(self, client):
            self.n += 1
            if self.n == 1:  # solo el 1er ciclo lista AL30
                return ({"24": {"AL30": Data912Row(symbol="AL30", c=99.0)}, "CI": {}},
                        {"AL30": "bonds"})
            return ({"24": {}, "CI": {}}, {})  # ausencia PERSISTENTE

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(handler))
        hub = ProviderHub(c)
        try:
            hub.set_source(_DropsAfterFirst())
            k = hub._K_ACTIVE_CYCLES                      # = 3
            snap = await hub.refresh_all()                # ciclo 1: activa lista 99.0
            assert snap["AL30"].c == 99.0
            # ciclos 2..k: dentro de la ventana → la activa retiene 99.0 (el floor NO pisa)
            for _ in range(k - 1):
                snap = await hub.refresh_all()
                assert snap["AL30"].c == 99.0
            # ciclo k+1: contador llegó a 0 → sale de _active_syms → el floor recupera 70.5
            snap = await hub.refresh_all()
            assert snap["AL30"].c == 70.5
        finally:
            await c.aclose()
    asyncio.run(run())


def test_data912_active_never_self_backstops():
    """Si la fuente activa YA es Data912 y vuelve vacía, no hay segundo fetch."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=[])  # todo vacío

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(handler))
        hub = ProviderHub(c)  # default Data912Source
        try:
            assert hub.active_mode == "data912"
            await hub.refresh_all()
            assert calls["n"] == 4  # 4 paneles Data912, sin backstop adicional
        finally:
            await c.aclose()
    asyncio.run(run())


def test_make_source_registry():
    assert isinstance(make_source("byma_open"), BymaOpenSource)
    assert isinstance(make_source("data912"), Data912Source)
    with pytest.raises(ValueError):
        make_source("nope")


def test_realtime_requires_credentials(monkeypatch):
    monkeypatch.delenv("BYMADATA_USER", raising=False)
    monkeypatch.delenv("BYMADATA_PASS", raising=False)
    assert BymaRealtimeSource.has_credentials() is False
    with pytest.raises(BymaRealtimeError):
        make_source("byma_realtime")


def test_realtime_available_with_credentials(monkeypatch):
    monkeypatch.setenv("BYMADATA_USER", "u")
    monkeypatch.setenv("BYMADATA_PASS", "p")
    assert BymaRealtimeSource.has_credentials() is True
    src = make_source("byma_realtime")
    assert src.mode == "byma_realtime"


# ── A4: freshness + purge ───────────────────────────────────────────────────

def test_freshness_tracks_active_source_symbols():
    """freshness() devuelve antigüedad en segundos solo para los símbolos que
    la fuente ACTIVA listó; NO marca symbols que vinieron del floor Data912."""
    async def run():
        c = ResilientClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
        hub = ProviderHub(c)
        try:
            hub.set_source(_FakeSource())         # entrega AL30
            assert hub.freshness() == {}          # sin datos aún
            await hub.refresh_all()
            f = hub.freshness()
            assert "AL30" in f
            assert f["AL30"] >= 0.0               # recién visto
        finally:
            await c.aclose()
    asyncio.run(run())


def test_stale_purge_on_day_rollover_and_empty_cycle_safe(monkeypatch):
    """Purga diaria: símbolo con timestamp > PURGE_MAX_DAYS se borra del snapshot
    al rollover del día. Un ciclo activo vacío NO dispara la purga (stale-safe)."""
    import time as _time

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
        hub = ProviderHub(c)
        try:
            hub.set_source(_FakeSource())
            await hub.refresh_all()               # AL30 vista hoy
            assert "AL30" in hub.snapshot()

            # Simular que AL30 fue vista hace PURGE_MAX_DAYS+1 días → debe purgarse.
            hub._seen_at["AL30"] = _time.monotonic() - (hub._PURGE_MAX_DAYS + 1) * 86400.0
            # Forzar rollover de día para que la purga corra.
            from datetime import date, timedelta
            hub._purge_day = date.today() - timedelta(days=1)

            # Ciclo con datos (seen_this_cycle no vacío) → dispara purge.
            await hub.refresh_all()
            # AL30 listada nuevamente por _FakeSource → se ve este ciclo; su timestamp
            # se actualiza ANTES de la purga en el lock, así que sobrevive.
            # Usamos un símbolo que NO aparece en _FakeSource para verificar la purga.
            hub._seen_at["DEADSTOCK"] = _time.monotonic() - (hub._PURGE_MAX_DAYS + 1) * 86400.0
            hub._snap[hub._snap and "24" or "24"].setdefault("DEADSTOCK",
                Data912Row(symbol="DEADSTOCK", c=10.0))
            hub._purge_day = date.today() - timedelta(days=1)
            await hub.refresh_all()
            assert "DEADSTOCK" not in hub.snapshot()  # purgado

            # Ciclo vacío NO purga (stale-safe).
            hub.set_source(_EmptySource())
            hub._seen_at["SAFE"] = _time.monotonic() - (hub._PURGE_MAX_DAYS + 1) * 86400.0
            hub._purge_day = date.today() - timedelta(days=1)
            await hub.refresh_all()               # vacío → seen_this_cycle empty → sin purge
            assert "SAFE" in hub._seen_at         # sobrevivió
        finally:
            await c.aclose()
    asyncio.run(run())
