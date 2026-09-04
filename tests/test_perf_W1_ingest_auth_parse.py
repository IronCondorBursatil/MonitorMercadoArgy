"""FASE 8 · W1 — el parseo pydantic de BYMA no puede correr en el event loop.

`BymaOpenSource.fetch` / `BymaRealtimeSource.fetch` instancian ~10k `Data912Row`
(~6,7us/fila ≈ 67ms) por ciclo. Ese mismo loop sirve el SSE y TODOS los requests
HTTP: bloquearlo 67ms cada 5s es latencia pura para el dashboard. El trabajo es
CPU puro sobre funciones ya puras (`byma_row_to_quote` / `settle_of`), así que va
a `asyncio.to_thread`.
"""

import asyncio
import json
import threading

import httpx
import pytest

from core.infrastructure.async_http import ResilientClient
from core.infrastructure.byma import sources as S

_PANEL_FLAGS = ("btnLideres", "btnGeneral", "btnCedears",
                "btnTitPublicos", "btnLetras", "btnObligNegociables")


def _rows_for(flag: str) -> list:
    """Filas crudas sintéticas: 3 por panel, con una basura sin symbol."""
    base = flag[3:6].upper()
    return [
        {"symbol": f"{base}1", "trade": 100.0, "settlementType": 2},
        {"symbol": f"{base}2", "trade": 200.0, "settlementType": 1},
        {"trade": 1.0},  # sin symbol → descartada
    ]


def _open_handler(req: httpx.Request) -> httpx.Response:
    body = json.loads(req.content)
    flag = next((k for k in _PANEL_FLAGS if body.get(k)), None)
    return httpx.Response(200, json={"content": {}, "data": _rows_for(flag or "btnX")})


def _rt_handler(req: httpx.Request) -> httpx.Response:
    body = json.loads(req.content)
    flag = next((k for k in _PANEL_FLAGS if body.get(k)), None)
    return httpx.Response(200, json={"data": _rows_for(flag or "btnX")})


def _spy_threads(monkeypatch) -> list:
    """Envuelve `byma_row_to_quote` y registra el thread de cada llamada."""
    seen: list = []
    real = S.byma_row_to_quote

    def spy(raw):
        seen.append(threading.get_ident())
        return real(raw)

    monkeypatch.setattr(S, "byma_row_to_quote", spy)
    return seen


def test_byma_open_parse_no_corre_en_el_event_loop(monkeypatch):
    seen = _spy_threads(monkeypatch)
    loop_thread = threading.get_ident()

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(_open_handler))
        try:
            return await S.BymaOpenSource().fetch(c)
        finally:
            await c.aclose()

    snaps, smap = asyncio.run(run())
    assert seen, "no se parseo ninguna fila (el fixture no ejercita el parseo)"
    assert smap, "el fetch no devolvio simbolos"
    offenders = [t for t in seen if t == loop_thread]
    assert not offenders, (
        f"{len(offenders)}/{len(seen)} filas se parsearon EN el event loop "
        f"(thread {loop_thread})")


def test_byma_realtime_parse_no_corre_en_el_event_loop(monkeypatch):
    seen = _spy_threads(monkeypatch)
    loop_thread = threading.get_ident()

    src = S.BymaRealtimeSource(username="u", password="p")
    src._token = "tok"
    src._expires_at = float("inf")

    async def run():
        c = ResilientClient(transport=httpx.MockTransport(_rt_handler))
        try:
            return await src.fetch(c)
        finally:
            await c.aclose()

    snaps, smap = asyncio.run(run())
    assert seen, "no se parseo ninguna fila (el fixture no ejercita el parseo)"
    assert smap, "el fetch no devolvio simbolos"
    offenders = [t for t in seen if t == loop_thread]
    assert not offenders, (
        f"{len(offenders)}/{len(seen)} filas se parsearon EN el event loop "
        f"(thread {loop_thread})")


@pytest.mark.parametrize("src_factory,handler", [
    (S.BymaOpenSource, _open_handler),
])
def test_parseo_offloop_preserva_el_resultado(src_factory, handler):
    """Contrato de datos: mover el parseo de thread no cambia snaps/smap."""
    async def run():
        c = ResilientClient(transport=httpx.MockTransport(handler))
        try:
            return await src_factory().fetch(c)
        finally:
            await c.aclose()

    snaps, smap = asyncio.run(run())
    # settlementType 2 → 24hs, 1 → CI. Bucket por panel.
    assert snaps["24"]["LID1"].c == 100.0
    assert snaps["CI"]["LID2"].c == 200.0
    assert smap["LID1"] == "stocks"
    assert smap["CED1"] == "cedears"
    assert smap["TIT1"] == "bonds"
    assert smap["LET1"] == "notes"
    assert smap["OBL1"] == "corp"
