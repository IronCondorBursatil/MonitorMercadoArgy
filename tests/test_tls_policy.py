"""Política de verificación TLS por host (M1.3 / QW4).

Verificar TLS SIEMPRE por default: la allowlist de cadena-rota arranca VACÍA.
La excepción histórica (open./addin.bymadata.com.ar, observada rota en 2026-06)
se re-verificó EN VIVO el 2026-09-03 con trust store certifi-only —el que usa
httpx en el droplet Linux— y los tres hosts BYMA encadenan OK contra
'GlobalSign RSA OV SSL CA 2018' (www 405, open 200/401, addin 401; control
negativo self-signed.badssl.com → CERTIFICATE_VERIFY_FAILED). Ver el docstring de
core/infrastructure/_tls.py. La perilla para exceptuar un host es el env
MONITOR_TLS_NO_VERIFY_HOSTS, nunca un `verify=False` hardcodeado."""

from __future__ import annotations

import pytest

from core.infrastructure._tls import should_verify, no_verify_hosts


@pytest.mark.parametrize("url", [
    "https://data912.com/live/arg_bonds",
    "https://api.bcra.gob.ar/estadisticas/v3.0/monetarias/1",
    "https://dolarapi.com/v1/dolares",
    "https://api.argentinadatos.com/v1/cotizaciones/dolares/",
    "https://estadisticas.cafci.org.ar/api/v1/fondo",
    "https://www.bymadata.com.ar/generic-oauth-core/oauth/token",
    # Los dos ex-exceptuados: hoy encadenan OK y NO deben saltear la verificación.
    "https://open.bymadata.com.ar/vanoms-be-core/rest/api/excel/byma/data/getLeadingEquity",
    "https://addin.bymadata.com.ar/vanoms-be-core/rest/api/excel/byma/data/getLeadingEquity",
])
def test_verifies_tls_for_healthy_hosts(url, monkeypatch):
    monkeypatch.delenv("MONITOR_TLS_NO_VERIFY_HOSTS", raising=False)
    assert should_verify(url) is True


def test_unknown_relative_url_defaults_to_verify():
    assert should_verify("/relative/path") is True
    assert should_verify("") is True


def test_env_override_replaces_allowlist(monkeypatch):
    monkeypatch.setenv("MONITOR_TLS_NO_VERIFY_HOSTS", "example.com, foo.test")
    assert set(no_verify_hosts()) == {"example.com", "foo.test"}
    assert should_verify("https://example.com/x") is False
    assert should_verify("https://open.bymadata.com.ar/x") is True  # no está en la lista


def test_env_override_puede_reponer_un_host_byma(monkeypatch):
    """La perilla operativa sigue viva: si mañana la cadena de BYMA se rompe de
    nuevo, el operador exceptúa el host por env sin tocar código."""
    monkeypatch.setenv("MONITOR_TLS_NO_VERIFY_HOSTS", "open.bymadata.com.ar")
    assert should_verify("https://open.bymadata.com.ar/x") is False
    assert should_verify("https://addin.bymadata.com.ar/x") is True


def test_default_allowlist_esta_vacia(monkeypatch):
    """Ningún host exceptuado por default: se verifica TLS en todos lados."""
    monkeypatch.delenv("MONITOR_TLS_NO_VERIFY_HOSTS", raising=False)
    assert no_verify_hosts() == ()
