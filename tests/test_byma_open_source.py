"""BymaOpenSource contra open.bymadata vía httpx.MockTransport (sin red).

Verifica el mapeo de campos, el tagging de bucket y el ruteo por plazo: renta fija
pide T0+T1 en una llamada → CI (settlementType 1) y 24hs (settlementType 2) separados.
"""

import asyncio
import json

import httpx

from core.infrastructure.async_http import ResilientClient
from core.infrastructure.byma.sources import BymaOpenSource

_PANEL_FLAGS = ("btnLideres", "btnGeneral", "btnCedears",
                "btnTitPublicos", "btnLetras", "btnObligNegociables")

_SAMPLE = {
    "btnLideres": [
        {"symbol": "GGAL", "trade": 7000.0, "bidPrice": 6990.0, "offerPrice": 7010.0,
         "volumeAmount": 1234567.0, "numberOfOrders": 42, "imbalance": -0.0356,
         "settlementType": 2},
        {"trade": 1.0},  # fila basura (sin symbol) → descartada
    ],
    "btnTitPublicos": [
        # mismo símbolo en los dos plazos (1=CI, 2=24hs) con precios distintos
        {"symbol": "AL30", "trade": 94110.0, "settlementType": 2},
        {"symbol": "AL30", "trade": 94070.0, "settlementType": 1},
        {"symbol": "AL30D", "trade": 64.35, "bidPrice": 64.30, "offerPrice": 64.40,
         "settlementType": 1},
    ],
    "btnObligNegociables": [
        {"symbol": "ZZC1O", "trade": 99.0, "settlementType": 2},
    ],
}


def _handler(req: httpx.Request) -> httpx.Response:
    assert req.method == "POST"
    assert "get-market-data" in str(req.url)
    assert req.headers.get("Token")
    assert req.headers.get("Options")
    body = json.loads(req.content)
    flag = next((k for k in _PANEL_FLAGS if body.get(k)), None)
    # renta fija pide ambos plazos en una sola llamada
    if flag in ("btnTitPublicos", "btnLetras", "btnObligNegociables"):
        assert body.get("T0") and body.get("T1")
    data = _SAMPLE.get(flag, [])
    return httpx.Response(200, json={"content": {}, "data": data})


def test_byma_open_source_dual_settlement_and_tags():
    async def run():
        c = ResilientClient(transport=httpx.MockTransport(_handler))
        try:
            snaps, smap = await BymaOpenSource().fetch(c)
            r24, rci = snaps["24"], snaps["CI"]
            # acción → 24hs, bucket stocks
            assert r24["GGAL"].c == 7000.0
            assert r24["GGAL"].px_bid == 6990.0
            assert abs(r24["GGAL"].pct_change - (-3.56)) < 1e-9
            assert smap["GGAL"] == "stocks"
            # soberano en AMBOS plazos, precios distintos
            assert r24["AL30"].c == 94110.0
            assert rci["AL30"].c == 94070.0
            assert smap["AL30"] == "bonds"
            # pata MEP solo CI en el sample
            assert rci["AL30D"].c == 64.35
            # ON → 24hs, bucket corp
            assert r24["ZZC1O"].c == 99.0
            assert smap["ZZC1O"] == "corp"
        finally:
            await c.aclose()
    asyncio.run(run())
