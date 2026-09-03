"""Hallazgos A-3 y A-4: clientes httpx del paquete `byma/` con `verify=False`
hardcodeado, esquivando `core.infrastructure._tls.should_verify`.

A-3 (security): `BymaRealtimeSource._ensure_token` manda usuario+password del
usuario (grant_type=password) y el Basic del client por un canal SIN verificar.
`www.bymadata.com.ar` NO está en la allowlist de cadena-rota: `should_verify()`
devuelve True, o sea que el código violaba la política vigente del repo
(tests/test_tls_policy.py:22 ya asserteaba que ese host debe verificarse).

A-4 (security, menor): `chart_history._request_closes` hardcodeaba `verify=False`
para open.bymadata.com.ar, dejando INERTE el override `MONITOR_TLS_NO_VERIFY_HOSTS`
sobre ese camino. Desde 2026-09-03 la allowlist default está VACÍA (los tres hosts
BYMA encadenan OK con certifi), así que ahí ahora se verifica de verdad.

Verificado en vivo (2026-09-03) con trust store certifi-only (emulando el droplet
Linux): www./open./addin.bymadata.com.ar validan los tres; el GET al chart de
'AL30D 24HS' devuelve 200 s='ok' con 41 puntos idéntico con verify=True y False.
"""

import asyncio

import httpx

from core.infrastructure.byma import chart_history
from core.infrastructure.byma.sources import BymaRealtimeSource


class _Resp:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {"access_token": "TOK", "expires_in": 3600}


def _fake_async_client(captured):
    class _Fake:
        def __init__(self, **kw):
            captured.append(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, headers=None):
            return _Resp()

    return _Fake


# --------------------------------------------------------------------- A-3 --

def test_ensure_token_verifica_tls_por_defecto(monkeypatch):
    """El POST OAuth (que lleva user/password) debe verificar la cadena TLS."""
    monkeypatch.delenv("MONITOR_TLS_NO_VERIFY_HOSTS", raising=False)
    captured = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(captured))

    src = BymaRealtimeSource(username="u", password="p")
    assert asyncio.run(src._ensure_token()) == "TOK"
    assert captured and captured[0].get("verify") is True


def test_ensure_token_respeta_el_override_de_tls(monkeypatch):
    """Si un operador declara el host como cadena-rota, se respeta (política única)."""
    monkeypatch.setenv("MONITOR_TLS_NO_VERIFY_HOSTS", "www.bymadata.com.ar")
    captured = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(captured))

    src = BymaRealtimeSource(username="u", password="p")
    assert asyncio.run(src._ensure_token()) == "TOK"
    assert captured and captured[0].get("verify") is False


# --------------------------------------------------------------------- A-4 --

def _fake_sync_client(captured):
    class _Fake:
        def __init__(self, **kw):
            captured.append(kw)

        def get(self, url, params=None, headers=None):
            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {"s": "no_data"}
            return _R()

        def close(self):
            return None

    return _Fake


def test_chart_history_respeta_el_override_de_tls(monkeypatch):
    """Con la allowlist vaciada, el priming de históricos DEBE verificar."""
    monkeypatch.setenv("MONITOR_TLS_NO_VERIFY_HOSTS", "")
    captured = []
    monkeypatch.setattr(chart_history.httpx, "Client", _fake_sync_client(captured))

    chart_history.fetch_history("AL30D")
    assert captured and captured[0].get("verify") is True


def test_chart_history_verifica_por_default(monkeypatch):
    """Sin override, open.bymadata.com.ar YA NO está exceptuado → verifica.
    (La allowlist default quedó vacía: ver core/infrastructure/_tls.py.)"""
    monkeypatch.delenv("MONITOR_TLS_NO_VERIFY_HOSTS", raising=False)
    captured = []
    monkeypatch.setattr(chart_history.httpx, "Client", _fake_sync_client(captured))

    chart_history.fetch_index_history("M")
    assert captured and captured[0].get("verify") is True


def test_chart_history_respeta_una_excepcion_puesta_por_env(monkeypatch):
    """La perilla operativa sigue viva sobre este camino (no `verify=False` fijo)."""
    monkeypatch.setenv("MONITOR_TLS_NO_VERIFY_HOSTS", "open.bymadata.com.ar")
    captured = []
    monkeypatch.setattr(chart_history.httpx, "Client", _fake_sync_client(captured))

    chart_history.fetch_index_history("M")
    assert captured and captured[0].get("verify") is False
