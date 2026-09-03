"""Hallazgo A-1: ResilientClient anulaba TODOS los timeouts del hot-path.

`_request_json` pasaba `timeout=None` explícito a `client.request()`. En httpx el
centinela de "usar el default del cliente" es `httpx.USE_CLIENT_DEFAULT`, NO `None`:
`None` significa `httpx.Timeout(None)` = sin connect/read/write/pool timeout.

MockTransport expone el timeout efectivo en `request.extensions["timeout"]`, así que
el test es determinista y sin red.
"""

import asyncio

import httpx

from core.infrastructure.async_http import ResilientClient


def _capture(seen):
    def handler(req):
        seen.append(req.extensions.get("timeout"))
        return httpx.Response(200, json={"ok": True})
    return handler


def test_get_json_sin_timeout_usa_el_default_del_cliente():
    """El hot-path (Data912Source.fetch, BymaOpenSource._panel, ...) llama sin
    `timeout`: debe heredar httpx.Timeout(4.0, connect=2.0, pool=2.0)."""
    seen = []

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(_capture(seen)), timeout=4.0)
        try:
            await c.get_json("https://data912.com/live/arg_bonds")
        finally:
            await c.aclose()

    asyncio.run(run())
    assert seen == [{"connect": 2.0, "read": 4.0, "write": 4.0, "pool": 2.0}]


def test_post_json_sin_timeout_usa_el_default_del_cliente():
    seen = []

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(_capture(seen)), timeout=4.0)
        try:
            await c.post_json("https://open.bymadata.com.ar/x", json={"a": 1})
        finally:
            await c.aclose()

    asyncio.run(run())
    assert seen == [{"connect": 2.0, "read": 4.0, "write": 4.0, "pool": 2.0}]


def test_timeout_explicito_del_caller_sigue_ganando():
    """FX/BCRA/REM pasan timeout=10.0 explícito: no se debe romper ese camino."""
    seen = []

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(_capture(seen)), timeout=4.0)
        try:
            await c.get_json("https://dolarapi.com/v1/dolares", timeout=10.0)
        finally:
            await c.aclose()

    asyncio.run(run())
    assert seen == [{"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}]


def test_construccion_custom_timeout_se_respeta():
    seen = []

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(_capture(seen)), timeout=7.5)
        try:
            await c.get_json("https://x.test/a")
        finally:
            await c.aclose()

    asyncio.run(run())
    assert seen == [{"connect": 2.0, "read": 7.5, "write": 7.5, "pool": 2.0}]
