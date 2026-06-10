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


def test_no_backstop_once_active_has_delivered():
    """Si la fuente activa ya entregó datos, un ciclo vacío posterior NO dispara el
    backstop: el stale-safe preserva el precio vivo, no lo piso con cierre previo."""
    calls = {"n": 0}

    def handler(request):
        if "data912.com" in str(request.url):
            calls["n"] += 1
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
            await hub.refresh_all()         # BYMA 99.0
            snap = await hub.refresh_all()  # vacío → stale-safe mantiene 99.0
            assert snap["AL30"].c == 99.0   # NO se pisó con cierre previo
            assert calls["n"] == 0          # backstop nunca se llamó
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
