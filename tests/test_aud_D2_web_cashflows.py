"""Auditoría D2 — `GET /cashflows?days=<n>` sin cota reventaba con 500.

`days: int = 180` entraba directo en `today + timedelta(days=days)`: apenas se pasa
de `date.max` (≈2.912.000 días) la suma tira `OverflowError`, que nadie atrapa
(app.py sólo registra un exception_handler para `RequiresLoginException`) → 500 con
traceback en el log en vez de degradar limpio. Pydantic valida el TIPO, no el rango.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.web.app import app


def test_days_gigante_no_revienta_el_router():
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/cashflows?days=999999999")
        assert r.status_code == 422, r.status_code       # rechazo limpio, no 500


def test_days_negativo_no_revienta_el_router():
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/cashflows?days=-1000000")
        assert r.status_code == 422, r.status_code


def test_days_razonable_sigue_andando():
    with TestClient(app) as c:
        assert c.get("/cashflows").status_code == 200
        assert c.get("/cashflows?days=30").status_code == 200
        assert c.get("/cashflows?days=3650").status_code == 200
