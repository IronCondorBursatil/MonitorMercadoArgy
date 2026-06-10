"""Política de verificación TLS por host (M1.3 / QW4).

Verificar TLS por defecto (seguro); saltear SOLO para hosts con cadena rota,
verificado empíricamente en vivo (2026-06): únicamente los endpoints addin/open
de BYMA fallan CERTIFICATE_VERIFY_FAILED. data912/BCRA/dolarapi/CAFCI/argentinadatos
verifican OK — el comentario viejo que los marcaba rotos estaba desactualizado."""

from __future__ import annotations

import pytest

from core.infrastructure._tls import should_verify, no_verify_hosts


@pytest.mark.parametrize("url", [
    "https://data912.com/live/arg_bonds",
    "https://api.bcra.gob.ar/estadisticas/v3.0/monetarias/1",
    "https://dolarapi.com/v1/dolares",
    "https://api.argentinadatos.com/v1/cotizaciones/dolares/",
    "https://estadisticas.cafci.org.ar/api/v1/fondo",
    "https://www.bymadata.com.ar/generic-oauth-core/oauth/token",  # token endpoint verifica OK
])
def test_verifies_tls_for_healthy_hosts(url):
    assert should_verify(url) is True


@pytest.mark.parametrize("url", [
    "https://open.bymadata.com.ar/vanoms-be-core/rest/api/excel/byma/data/getLeadingEquity",
    "https://addin.bymadata.com.ar/vanoms-be-core/rest/api/excel/byma/data/getLeadingEquity",
])
def test_skips_verification_for_broken_chain_hosts(url):
    assert should_verify(url) is False


def test_unknown_relative_url_defaults_to_verify():
    assert should_verify("/relative/path") is True
    assert should_verify("") is True


def test_env_override_replaces_allowlist(monkeypatch):
    monkeypatch.setenv("MONITOR_TLS_NO_VERIFY_HOSTS", "example.com, foo.test")
    assert set(no_verify_hosts()) == {"example.com", "foo.test"}
    assert should_verify("https://example.com/x") is False
    assert should_verify("https://open.bymadata.com.ar/x") is True  # ya no está en la lista


def test_default_allowlist_is_only_broken_byma(monkeypatch):
    monkeypatch.delenv("MONITOR_TLS_NO_VERIFY_HOSTS", raising=False)
    assert set(no_verify_hosts()) == {"open.bymadata.com.ar", "addin.bymadata.com.ar"}


def test_sync_noverify_client_does_not_follow_redirects():
    """F10 (review): el cliente se elige por la URL INICIAL; con follow_redirects el
    cliente no-verify seguiría un redirect cross-host SIN verificar TLS en el host
    destino. El no-verify no debe seguir redirects (falla visible > fetch inseguro
    silencioso); el verificado sí puede (verificar de más es seguro)."""
    from core.infrastructure import _http

    assert _http._client.follow_redirects is True
    assert _http._client_noverify.follow_redirects is False


def test_sync_clients_share_config_except_verify():
    """F11 (review): ambos clientes salen de la misma factory — mismos
    timeout/limits; solo difiere verify (anti-drift)."""
    from core.infrastructure import _http

    assert _http._client.timeout == _http._client_noverify.timeout
