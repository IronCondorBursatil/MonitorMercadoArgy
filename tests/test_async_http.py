"""ResilientClient + ProviderHub (Fase 3) con httpx.MockTransport (sin red)."""

import asyncio

import httpx
import pytest

from core.infrastructure.async_http import ResilientClient
from core.infrastructure.circuit_breaker import CircuitOpenError
from core.infrastructure.provider_hub import HubMarketDataProvider, ProviderHub


class _StubHistory:
    """history_provider mínimo para HubMarketDataProvider (sin red)."""
    def fetch_historical_prices(self, ticker, days):
        return {}

    def fetch_stock_history(self, ticker):
        return []


def test_get_json_ok():
    def handler(req):
        return httpx.Response(200, json=[{"symbol": "AL30", "c": 100.5}])

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(handler))
        try:
            assert await c.get_json("https://data912.com/live/arg_bonds") == [{"symbol": "AL30", "c": 100.5}]
        finally:
            await c.aclose()
    asyncio.run(run())


def test_retry_then_success_on_503():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(handler))
        try:
            assert await c.get_json("https://x.test/a", retries=1) == {"ok": True}
            assert calls["n"] == 2
        finally:
            await c.aclose()
    asyncio.run(run())


def test_breaker_opens_after_repeated_failures():
    def handler(req):
        return httpx.Response(500)

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(handler), fail_max=2, reset_timeout=30)
        try:
            for _ in range(2):
                with pytest.raises(httpx.HTTPStatusError):
                    await c.get_json("https://y.test/a", retries=0)
            with pytest.raises(CircuitOpenError):  # breaker abierto
                await c.get_json("https://y.test/a", retries=0)
        finally:
            await c.aclose()
    asyncio.run(run())


def test_provider_hub_validates_and_merges():
    def handler(req):
        if "arg_bonds" in str(req.url):
            # una fila válida + una corrupta (sin 'c')
            return httpx.Response(200, json=[{"symbol": "al30", "c": 100.0}, {"symbol": "bad"}])
        return httpx.Response(200, json=[])

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(handler))
        hub = ProviderHub(c)
        try:
            snap = await hub.fetch_data912()
            assert "AL30" in snap and snap["AL30"].c == 100.0  # validada + uppercased
            assert "BAD" not in snap                            # fila corrupta descartada
        finally:
            await c.aclose()
    asyncio.run(run())


def test_provider_hub_preserves_stale_on_total_failure():
    state = {"fail": False}

    def handler(req):
        if state["fail"]:
            return httpx.Response(503)
        return httpx.Response(200, json=[{"symbol": "AL30", "c": 100.0}])

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(handler))
        hub = ProviderHub(c)
        try:
            assert "AL30" in await hub.fetch_data912()
            state["fail"] = True
            assert "AL30" in await hub.fetch_data912()  # stale preservado, no wipe
        finally:
            await c.aclose()
    asyncio.run(run())


def test_refresh_all_feeds_hub_market_data_provider():
    """refresh_all() puebla el hub y HubMarketDataProvider sirve MarketSnapshots
    desde ese snapshot (incluido el alias _CER de los duales)."""
    def handler(req):
        if "arg_bonds" in str(req.url):
            return httpx.Response(200, json=[
                {"symbol": "TXMJ8", "c": 142.5, "px_bid": 142.0, "px_ask": 143.0},
            ])
        return httpx.Response(200, json=[])

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(handler))
        hub = ProviderHub(c)
        try:
            await hub.refresh_all()
            provider = HubMarketDataProvider(hub, history_provider=_StubHistory())
            snaps = provider.fetch_snapshots(["TXMJ8", "TXMJ8_CER", "NOPE"])
            assert snaps["TXMJ8"].price == 142.5
            assert snaps["TXMJ8"].bid == 142.0
            assert snaps["TXMJ8_CER"].price == 142.5   # alias → mismo subyacente
            assert "NOPE" not in snaps                   # sin fila → omitido
        finally:
            await c.aclose()
    asyncio.run(run())
