"""Smoke de la app FastAPI (Fase 4 fundación): boot vía lifespan + rutas health/metrics.

No toca la red: el refresh loop hace su primer tick recién a `refresh_sec`, así que
un test sub-segundo nunca dispara fetch. El catálogo se lee de SQLite (CatalogRepository).
"""

from fastapi.testclient import TestClient

from apps.web.app import app


def test_health_boots_and_loads_catalog():
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        # Bajo test los loops no corren → nunca hubo refresh → "degraded" (O1).
        # En operación normal pasa a "ok" tras el primer ciclo (≤5s).
        assert data["status"] in ("ok", "degraded")
        assert data["is_stale"] is True                  # never refreshed
        assert data["instruments"] >= 80, data           # ~91 instrumentos seedeados
        assert "last_refresh" in data and "metrics_cached" in data


def test_metrics_route_shape():
    with TestClient(app) as client:
        r = client.get("/api/metrics")
        assert r.status_code == 200
        assert isinstance(r.json(), list)  # lista (vacía hasta el primer refresh)
