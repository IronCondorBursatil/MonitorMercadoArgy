"""Remediación R2 (residuo del hallazgo A-2): el negative caching de
`DolarAPIProvider` cubría SOLO el camino de excepción.

`tests/test_aud_A_infra_http_fx_negative_cache.py` cierra el caso "dolarapi tira
error" (timeout / 5xx / DNS). Pero un upstream DEGRADADO —200 con `[]`, o filas sin
`casa`, que es lo que devuelve un WAF o un endpoint en mantenimiento— NO lanza
excepción: `_process_payload` salía con `fresh` vacío sin estampar `_last_fetch_ts`
ni `_last_fail_ts`, así que la tormenta de requests seguía intacta.

Por qué importa: `get_quote()` se llama POR INSTRUMENTO dentro del motor
(~342 veces por ciclo vía DolarLinked/HardDollar), cada una con
`httpx.get(timeout=10.0)` bajo el lock de CLASE. Medido antes del fix: 50 lecturas
con el upstream devolviendo 200 + `[]` = 50 GETs. El ciclo de 5s pasaba a minutos y
bloqueaba además los handlers sync (/header/cards).

Invariante que fijan estos tests: **toda** salida de `_fetch` que no deje una
cotización usable estampa el sello de fallo, venga de una excepción o de un 200
inservible. La cache previa NO se pisa (se sigue sirviendo stale).
"""

import time

import pytest

from core.infrastructure import fx_provider as fx_mod
from core.infrastructure.fx_provider import DolarAPIProvider


@pytest.fixture(autouse=True)
def _reset_fx_class_state():
    prev = (DolarAPIProvider._cache, DolarAPIProvider._last_fetch_ts,
            DolarAPIProvider._last_fail_ts)
    DolarAPIProvider._cache = {}
    DolarAPIProvider._last_fetch_ts = 0.0
    DolarAPIProvider._last_fail_ts = 0.0
    yield
    (DolarAPIProvider._cache, DolarAPIProvider._last_fetch_ts,
     DolarAPIProvider._last_fail_ts) = prev


def _counting_200(monkeypatch, payload):
    """`httpx.get` que siempre contesta 200 con `payload` y cuenta las llamadas."""
    calls = {"n": 0}

    class _Resp:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return payload

    def _get(*a, **kw):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(fx_mod.httpx, "get", _get)
    return calls


# Los dos sabores de "200 inservible" observables en dolarapi.
_PAYLOADS_INUTILES = [
    pytest.param([], id="lista-vacia"),
    pytest.param([{"nombre": "Mayorista", "venta": 1400}], id="filas-sin-casa"),
    pytest.param([{"casa": "", "venta": 1400}, {"casa": "   "}], id="casa-vacia"),
]


@pytest.mark.parametrize("payload", _PAYLOADS_INUTILES)
def test_200_sin_cotizaciones_usables_no_hace_un_get_por_instrumento(monkeypatch, payload):
    """El síntoma exacto del hallazgo: 50 lecturas → 1 sola salida a la red."""
    calls = _counting_200(monkeypatch, payload)
    fx = DolarAPIProvider()
    for _ in range(50):
        assert fx.get_mayorista_venta() is None
    assert calls["n"] == 1, (
        f"200 inservible ({payload!r}) no estampó el sello de fallo: "
        f"{calls['n']} GETs en 50 lecturas")


@pytest.mark.parametrize("payload", _PAYLOADS_INUTILES)
def test_200_inutil_estampa_el_sello_de_fallo(monkeypatch, payload):
    """Aserción directa sobre el mecanismo (no solo sobre el conteo)."""
    _counting_200(monkeypatch, payload)
    DolarAPIProvider().get_all()
    assert DolarAPIProvider._last_fail_ts > 0.0
    assert DolarAPIProvider._last_fetch_ts == 0.0   # no se marcó como fetch exitoso


def test_200_inutil_no_pisa_la_cache_previa(monkeypatch):
    """Un upstream degradado no puede borrar la última cotización buena."""
    DolarAPIProvider._cache = {"mayorista": {"venta": 1000.0, "compra": 990.0}}
    DolarAPIProvider._last_fetch_ts = time.time() - (DolarAPIProvider.TTL_SECONDS + 10)
    calls = _counting_200(monkeypatch, [])

    fx = DolarAPIProvider()
    for _ in range(50):
        assert fx.get_mayorista_venta() == 1000.0   # sigue sirviendo stale
    assert calls["n"] == 1


def test_el_cooldown_del_200_inutil_no_es_permanente(monkeypatch):
    """Pasado FAIL_COOLDOWN se reintenta (si no, un hipo de dolarapi congelaría el FX)."""
    calls = _counting_200(monkeypatch, [])
    fx = DolarAPIProvider()
    fx.get_mayorista_venta()
    assert calls["n"] == 1
    DolarAPIProvider._last_fail_ts = time.time() - (DolarAPIProvider.FAIL_COOLDOWN + 1)
    fx.get_mayorista_venta()
    assert calls["n"] == 2


def test_un_200_util_posterior_limpia_el_sello(monkeypatch):
    """Recuperación: tras un payload bueno el cooldown deja de bloquear."""
    _counting_200(monkeypatch, [])
    fx = DolarAPIProvider()
    fx.get_mayorista_venta()
    assert DolarAPIProvider._last_fail_ts > 0.0

    _counting_200(monkeypatch, [{"casa": "mayorista", "nombre": "May",
                                 "compra": 1390, "venta": 1400}])
    DolarAPIProvider._last_fail_ts = time.time() - (DolarAPIProvider.FAIL_COOLDOWN + 1)
    assert fx.get_mayorista_venta() == 1400.0
    assert DolarAPIProvider._last_fail_ts == 0.0
    assert DolarAPIProvider._last_fetch_ts > 0.0


def test_invalidate_cache_fuerza_pese_al_200_inutil(monkeypatch):
    """`invalidate_cache()` (scripts/data_quality_check.py) sigue significando forzar."""
    calls = _counting_200(monkeypatch, [])
    fx = DolarAPIProvider()
    fx.get_all()
    assert calls["n"] == 1
    fx.invalidate_cache()
    fx.get_all()
    assert calls["n"] == 2


def test_prefetch_async_tambien_sella_el_200_inutil():
    """El camino async del hub (una llamada por ciclo) comparte el sello: si no,
    el ciclo siguiente lo borraría y el camino sync volvería a la tormenta."""
    import asyncio

    class _Client:
        async def get_json(self, url, timeout=None, source=None):
            return []

    asyncio.run(DolarAPIProvider().prefetch(_Client()))
    assert DolarAPIProvider._last_fail_ts > 0.0
