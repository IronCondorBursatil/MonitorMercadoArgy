"""Indicador de staleness en el header (M2.1/O1): /health/badge."""

from fastapi.testclient import TestClient

from apps.web.app import app


def test_health_badge_route_registered():
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/health/badge" in paths


def test_badge_shows_stale_when_never_refreshed():
    # Bajo test los loops están deshabilitados → el state nunca refresca → stale.
    with TestClient(app) as c:
        r = c.get("/health/badge")
        assert r.status_code == 200
        assert ("datos viejos" in r.text) or ("sin datos" in r.text), \
            "el badge debe señalar datos viejos cuando no hubo refresh"


def test_api_health_reports_degraded_when_stale():
    with TestClient(app) as c:
        r = c.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "degraded"   # nunca refrescado → degradado
        assert body["is_stale"] is True
        assert "age_seconds" in body
        # `last_error` NO va en el payload público (filtra strings crudos de excepción
        # con URLs/params internos); el detalle lo muestra el badge, detrás de login.
        assert "last_error" not in body
