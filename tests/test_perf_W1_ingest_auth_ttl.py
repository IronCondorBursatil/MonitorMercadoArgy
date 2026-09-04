"""FASE 8 · W1 — TTL diferenciado para los paneles de renta variable de BYMA open.

Hoy los 6 POST (`page_size=5000`) salen CADA ciclo de 5s. `btnCedears` y
`btnGeneral` son los dos más pesados y NINGÚN panel del monitor los usa en vivo:
alimentan el reconcile de catálogo y el sidebar del ABM. Se cachean
`settings.equities_refresh_sec` segundos.

Invariantes que el cache NO puede romper:
  - `btnLideres` va a ciclo completo (lo consume `panel_lider`), igual que los 3
    de renta fija.
  - Los símbolos cacheados se RE-MERGEAN cada ciclo: si desaparecieran del
    snapshot, el floor Data912 y la ventana K del hub verían una fuente que
    "dejó de listarlos".
"""

import asyncio
import json
import time

import httpx

from config.settings import settings
from core.infrastructure.async_http import ResilientClient
from core.infrastructure.byma import sources as S

_PANEL_FLAGS = ("btnLideres", "btnGeneral", "btnCedears",
                "btnTitPublicos", "btnLetras", "btnObligNegociables")

_SLOW = ("btnGeneral", "btnCedears")
_FAST = ("btnLideres", "btnTitPublicos", "btnLetras", "btnObligNegociables")


def _rows_for(flag: str) -> list:
    base = flag[3:6].upper()
    return [
        {"symbol": f"{base}1", "trade": 100.0, "settlementType": 2},
        {"symbol": f"{base}2", "trade": 200.0, "settlementType": 1},
    ]


class _CountingHandler:
    def __init__(self):
        self.posts: dict[str, int] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        flag = next((k for k in _PANEL_FLAGS if body.get(k)), "?")
        self.posts[flag] = self.posts.get(flag, 0) + 1
        return httpx.Response(200, json={"content": {}, "data": _rows_for(flag)})


def _fetch(src, handler):
    async def run():
        c = ResilientClient(transport=httpx.MockTransport(handler))
        try:
            return await src.fetch(c)
        finally:
            await c.aclose()
    return asyncio.run(run())


def test_settings_expone_equities_refresh_sec():
    assert getattr(settings, "equities_refresh_sec", None) == 30


def test_cedears_y_general_no_re_postean_dentro_del_ttl(monkeypatch):
    monkeypatch.setattr(settings, "equities_refresh_sec", 999)
    h = _CountingHandler()
    src = S.BymaOpenSource()

    _fetch(src, h)
    snaps2, smap2 = _fetch(src, h)

    for flag in _SLOW:
        assert h.posts[flag] == 1, f"{flag} re-POSTeó dentro del TTL: {h.posts}"
    for flag in _FAST:
        assert h.posts[flag] == 2, f"{flag} NO va a ciclo completo: {h.posts}"

    # Re-merge del sub-snapshot cacheado: los símbolos NO desaparecen.
    assert snaps2["24"]["CED1"].c == 100.0
    assert snaps2["CI"]["CED2"].c == 200.0
    assert smap2["CED1"] == "cedears"
    assert snaps2["24"]["GEN1"].c == 100.0
    assert smap2["GEN1"] == "stocks"
    # ...y los de ciclo completo siguen ahí.
    assert snaps2["24"]["LID1"].c == 100.0
    assert snaps2["24"]["TIT1"].c == 100.0


def test_ttl_vencido_fuerza_el_re_post(monkeypatch):
    monkeypatch.setattr(settings, "equities_refresh_sec", 30)
    h = _CountingHandler()
    src = S.BymaOpenSource()

    _fetch(src, h)
    assert h.posts["btnCedears"] == 1

    # Reloj adelantado más allá del TTL (mismo salto para todos los timers).
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 31.0)
    snaps, smap = _fetch(src, h)

    assert h.posts["btnCedears"] == 2, f"el TTL vencido no re-POSTeó: {h.posts}"
    assert h.posts["btnGeneral"] == 2
    assert snaps["24"]["CED1"].c == 100.0


def test_ttl_cero_desactiva_el_cache(monkeypatch):
    """Perilla de apagado: `equities_refresh_sec=0` → comportamiento de hoy."""
    monkeypatch.setattr(settings, "equities_refresh_sec", 0)
    h = _CountingHandler()
    src = S.BymaOpenSource()
    _fetch(src, h)
    _fetch(src, h)
    assert all(h.posts[f] == 2 for f in _PANEL_FLAGS), h.posts


def test_panel_caido_no_deja_cache_envenenado(monkeypatch):
    """Si el POST de un panel lento falla (lista vacía), NO se cachea la nada:
    el ciclo siguiente reintenta en vez de servir un panel vacío 30s."""
    monkeypatch.setattr(settings, "equities_refresh_sec", 999)
    h = _CountingHandler()

    def failing(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        flag = next((k for k in _PANEL_FLAGS if body.get(k)), "?")
        h.posts[flag] = h.posts.get(flag, 0) + 1
        if flag == "btnCedears":
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"content": {}, "data": _rows_for(flag)})

    src = S.BymaOpenSource()
    _fetch(src, failing)
    tras_uno = h.posts["btnCedears"]          # ResilientClient reintenta el 5xx
    _fetch(src, failing)
    assert h.posts["btnCedears"] > tras_uno,         f"panel caído quedó cacheado vacío: {h.posts}"
    assert h.posts["btnGeneral"] == 1, "el panel sano SÍ debe quedar cacheado"


def test_cacheado_no_pisa_un_simbolo_del_panel_fresco(monkeypatch):
    """`btnGeneral` (cacheado) va DESPUÉS de `btnLideres` en `PANELS`. Si un
    símbolo está en los dos, el valor STALE no puede pisar al fresco."""
    monkeypatch.setattr(settings, "equities_refresh_sec", 999)
    state = {"lid": 100.0}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        flag = next((k for k in _PANEL_FLAGS if body.get(k)), "?")
        if flag in ("btnLideres", "btnGeneral"):
            data = [{"symbol": "GGAL", "trade": state["lid"], "settlementType": 2}]
        else:
            data = _rows_for(flag)
        return httpx.Response(200, json={"content": {}, "data": data})

    src = S.BymaOpenSource()
    _fetch(src, handler)
    state["lid"] = 7777.0            # GGAL se movió; solo btnLideres re-POSTea
    snaps, _ = _fetch(src, handler)
    assert snaps["24"]["GGAL"].c == 7777.0, "el panel cacheado pisó al fresco"
