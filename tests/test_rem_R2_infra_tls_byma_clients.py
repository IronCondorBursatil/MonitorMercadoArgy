"""Remediación R2 (residuo del hallazgo A-4): los dos clientes httpx de `byma/`
que habían quedado con `verify=False` HARDCODEADO.

`core/infrastructure/byma/series_historicas.py::fetch_history` (priming de cierres
diarios) y `core/infrastructure/byma/catalog_products.py::_post` (cauciones +
SENEBI-ON) creaban su cliente propio con `verify=False` fijo. Con la allowlist
default de entonces el efecto de red era el mismo que la política del repo, pero:

  1. el override operativo `MONITOR_TLS_NO_VERIFY_HOSTS` quedaba INERTE en esos dos
     caminos (no había forma de subir ni bajar la verificación sin tocar código), y
  2. al vaciarse la allowlist default (2026-09-03: los tres hosts BYMA encadenan OK
     con trust store certifi-only) esos dos clientes seguían MITM-eables.

Estos tests son la red de regresión: capturan los kwargs con que se construye el
cliente y exigen que salgan de `_tls.should_verify`, en las dos direcciones.
"""

from datetime import date

import pytest

from core.infrastructure.byma import catalog_products as cp
from core.infrastructure.byma import series_historicas as sh


class _R:
    status_code = 200
    text = "{}"

    @staticmethod
    def json():
        return {"data": []}


def _fake_client_factory(captured):
    """Reemplazo de `httpx.Client` que registra los kwargs de construcción."""

    class _Fake:
        def __init__(self, **kw):
            captured.append(kw)

        def post(self, url, json=None, headers=None):
            return _R()

        def get(self, url, params=None, headers=None):
            return _R()

        def close(self):
            return None

    return _Fake


# ------------------------------------------------------- series_historicas --

def test_series_historicas_verifica_tls_por_default(monkeypatch):
    monkeypatch.delenv("MONITOR_TLS_NO_VERIFY_HOSTS", raising=False)
    captured = []
    monkeypatch.setattr(sh.httpx, "Client", _fake_client_factory(captured))

    sh.fetch_history("AL30D", max_days=30)
    assert captured, "fetch_history no construyó su cliente propio"
    assert captured[0].get("verify") is True


def test_series_historicas_respeta_el_override_de_tls(monkeypatch):
    """Si un operador declara el host como cadena-rota, este camino lo obedece."""
    monkeypatch.setenv("MONITOR_TLS_NO_VERIFY_HOSTS", "open.bymadata.com.ar")
    captured = []
    monkeypatch.setattr(sh.httpx, "Client", _fake_client_factory(captured))

    sh.fetch_history("AL30D", max_days=30)
    assert captured and captured[0].get("verify") is False


def test_series_historicas_no_toca_el_cliente_inyectado(monkeypatch):
    """El `client=` inyectable (tests / reuso de conexión) sigue mandando."""
    monkeypatch.setenv("MONITOR_TLS_NO_VERIFY_HOSTS", "open.bymadata.com.ar")
    captured = []
    monkeypatch.setattr(sh.httpx, "Client", _fake_client_factory(captured))

    class _Inj:
        def post(self, url, json=None, headers=None):
            return _R()

    assert sh.fetch_history("AL30D", max_days=30, client=_Inj()) == {}
    assert captured == []          # no construyó cliente propio


# -------------------------------------------------------- catalog_products --

@pytest.mark.parametrize("fn", [cp.fetch_cauciones, cp.fetch_senebi_on])
def test_catalog_products_verifica_tls_por_default(fn, monkeypatch):
    monkeypatch.delenv("MONITOR_TLS_NO_VERIFY_HOSTS", raising=False)
    captured = []
    monkeypatch.setattr(cp.httpx, "Client", _fake_client_factory(captured))

    fn()
    assert captured, "el fetch no construyó su cliente propio"
    assert captured[0].get("verify") is True


def test_catalog_products_respeta_el_override_de_tls(monkeypatch):
    monkeypatch.setenv("MONITOR_TLS_NO_VERIFY_HOSTS", "open.bymadata.com.ar")
    captured = []
    monkeypatch.setattr(cp.httpx, "Client", _fake_client_factory(captured))

    cp.fetch_cauciones()
    assert captured and captured[0].get("verify") is False


def test_ningun_cliente_byma_hardcodea_verify_false():
    """Guard de repo (AST, no grep): ninguna llamada del paquete `byma/` pasa
    `verify=False` literal.

    Es el defecto de CLASE del hallazgo A-3/A-4 (cuatro archivos lo tenían). La
    política vive en un solo lugar (`_tls.should_verify`); un `verify=False` fijo
    la esquiva en silencio y no lo atrapa ningún test de comportamiento mientras el
    host esté en la allowlist — recién muerde el día que la allowlist cambia, que
    es exactamente lo que pasó el 2026-09-03."""
    import ast
    from pathlib import Path

    ofensores = []
    for f in sorted(Path(sh.__file__).parent.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "verify":
                    continue
                if isinstance(kw.value, ast.Constant) and kw.value.value is False:
                    ofensores.append(f"{f.name}:{node.lineno}")
    assert ofensores == []


def test_fetch_history_usa_la_fecha_de_hoy_como_tope(monkeypatch):
    monkeypatch.delenv("MONITOR_TLS_NO_VERIFY_HOSTS", raising=False)
    bodies = []

    class _Cli:
        def post(self, url, json=None, headers=None):
            bodies.append(json)
            return _R()

    sh.fetch_history("AL30D", max_days=10, client=_Cli())
    assert bodies and bodies[0]["toDate"] == date.today().isoformat()
