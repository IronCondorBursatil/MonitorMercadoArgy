"""BymaRealtimeSource: ruteo de paneles (regresión del bug de ONs) — sin red.

El endpoint dedicado `getObligacionesNegociables` 400ea ("No se pudo determinar el
panel"); las ONs deben ir por `getLeadingEquity` + `btnObligNegociables`. Estos
tests congelan esa config para que no regrese."""

import asyncio
import json

import httpx

from core.infrastructure.async_http import ResilientClient
from core.infrastructure.byma.sources import BymaOpenSource, BymaRealtimeSource

_FLAGS = ("btnLideres", "btnGeneral", "btnCedears",
          "btnTitPublicos", "btnLetras", "btnObligNegociables")


def test_realtime_routes_ons_through_leading_equity(monkeypatch):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append(url)
        # el endpoint roto NUNCA debe llamarse
        assert "getObligacionesNegociables" not in url
        body = json.loads(req.content)
        flag = next((k for k in _FLAGS if body.get(k)), None)
        if flag == "btnObligNegociables":
            data = [{"symbol": "ZZC1O", "trade": 99.0, "imbalance": 0.0}]
        elif flag == "btnLideres":
            data = [{"symbol": "GGAL", "trade": 7000.0}]
        else:
            data = []
        return httpx.Response(200, json=data)

    async def run():
        src = BymaRealtimeSource(username="u", password="p")

        async def _fake_token(force=False):
            return "tok"

        monkeypatch.setattr(src, "_ensure_token", _fake_token)
        c = ResilientClient(transport=httpx.MockTransport(handler))
        try:
            snaps, smap = await src.fetch(c)
            rows = snaps["24"]                        # sin settlementType → 24hs
            assert "ZZC1O" in rows and "GGAL" in rows
            assert smap.get("ZZC1O") == "corp"        # ONs cubiertas
            assert smap.get("GGAL") == "stocks"
            assert any("getLeadingEquity" in u for u in calls)
        finally:
            await c.aclose()

    asyncio.run(run())


def test_realtime_panels_avoid_broken_on_endpoint():
    eps = [ep for ep, *_ in BymaRealtimeSource.PANELS]
    assert not any("getObligacionesNegociables" in e for e in eps)
    buckets = {b for *_, b in BymaRealtimeSource.PANELS}
    assert {"stocks", "cedears", "bonds", "notes", "corp"} <= buckets


def test_open_panels_cover_all_buckets():
    buckets = {b for *_, b in BymaOpenSource.PANELS}
    assert {"stocks", "cedears", "bonds", "notes", "corp"} <= buckets
    # 24hs (T1) en todos; renta fija (bonds/notes/corp) además pide CI (T0) en la misma
    # llamada → un solo fetch trae ambos plazos.
    for _flag, _seg, plazos, bucket in BymaOpenSource.PANELS:
        assert "T1" in plazos
        if bucket in ("bonds", "notes", "corp"):
            assert "T0" in plazos
