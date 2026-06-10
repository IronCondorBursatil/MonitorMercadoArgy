"""ProviderHub.fetch_options: BYMA open /options (sin token) + fallback Data912.

Verifica que el panel BYMA da OI real + underlyingSymbol/optionType/maturityDate
y que los subyacentes salen del feed live del hub; y que ante BYMA vacío cae a
Data912. Sin red (httpx.MockTransport)."""

import asyncio
import json

import httpx

from core.infrastructure.async_http import ResilientClient
from core.infrastructure.byma.field_map import SETTLE_24
from core.infrastructure.provider_hub import ProviderHub
from core.infrastructure.schemas import Data912Row

_BYMA_OPTIONS = [
    {"symbol": "GFGC70000AG", "trade": 480.0, "bidPrice": 475.0, "offerPrice": 485.0,
     "openInterest": 1200.0, "optionType": "CALL", "underlyingSymbol": "GGAL",
     "maturityDate": "2026-08-21", "volumeAmount": 50000.0, "numberOfOrders": 10,
     "settlementType": 2},
    {"symbol": "VISV9000AG", "trade": 30.0, "openInterest": 50.0, "optionType": "PUT",
     "underlyingSymbol": "VIST", "maturityDate": "2026-08-21", "settlementType": 2},
    {"symbol": "AL30", "trade": 100.0, "settlementType": 2},  # sin underlying → no opción
]


def _seed_live_stocks(hub: ProviderHub) -> None:
    rows = {"GGAL": Data912Row(symbol="GGAL", c=70000.0),
            "VIST": Data912Row(symbol="VIST", c=9000.0)}
    hub._merge({SETTLE_24: rows}, {"GGAL": "stocks", "VIST": "stocks"})


def test_fetch_options_byma_real_oi_and_live_stocks():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST" and str(req.url).endswith("/options")
        assert not req.headers.get("Token")          # panel /options NO requiere token
        return httpx.Response(200, json={"content": {}, "data": _BYMA_OPTIONS})

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(handler))
        try:
            hub = ProviderHub(c)
            _seed_live_stocks(hub)
            opt, stk = await hub.fetch_options("byma")
            # La fila sin underlyingSymbol (AL30) se descarta: no es una opción.
            assert set(opt) == {"GFGC70000AG", "VISV9000AG"}
            assert opt["GFGC70000AG"].oi == 1200.0           # OI REAL
            assert opt["GFGC70000AG"].opt_underlying == "GGAL"
            assert opt["VISV9000AG"].opt_kind == "V"
            # subyacentes desde el feed live del hub
            assert set(stk) == {"GGAL", "VIST"}
        finally:
            await c.aclose()
    asyncio.run(run())


def test_fetch_options_falls_back_to_data912_when_byma_empty():
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/options"):
            return httpx.Response(200, json={"content": {}, "data": []})  # BYMA vacío
        if "arg_options" in url:
            return httpx.Response(200, json=[{"symbol": "GFGC70000AG", "c": 480.0,
                                              "px_bid": 475.0, "px_ask": 485.0, "q_op": 7}])
        if "arg_stocks" in url:
            return httpx.Response(200, json=[{"symbol": "GGAL", "c": 70000.0}])
        return httpx.Response(404, json={})

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(handler))
        try:
            hub = ProviderHub(c)
            opt, stk = await hub.fetch_options("byma")
            assert "GFGC70000AG" in opt          # vino de Data912 (fallback)
            assert opt["GFGC70000AG"].oi is None  # Data912 no trae OI real
            assert "GGAL" in stk
        finally:
            await c.aclose()
    asyncio.run(run())
