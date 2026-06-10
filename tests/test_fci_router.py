"""Router FCI: la página carga la app cliente y /fci/data devuelve el dataset JSON.

Sin red: se inyecta el dataset class-level del CAFCIProvider (gate cerrado), se mockea
el fetch de ArgentinaDatos y se stubean indices/fx vía dependency_overrides."""

from datetime import date

from fastapi.testclient import TestClient

import apps.web.fci_service as fci_service
from apps.web.app import app
from apps.web.deps import get_fx, get_indices
from core.infrastructure.cafci_provider import CAFCIProvider, _parse_payload

_PAYLOAD = {
    "catalogo": {"fondos": [{
        "id": 1, "nombre": "Test MM", "estado": 1, "tipo_dinero": "Clásico",
        "dias_liquidacion": 0, "sociedad_gerente": {"nombre": "Soc"},
        "moneda": {"nombre": "Peso Argentina"}, "tipo_renta": {"nombre": "Mercado de Dinero"},
        "clases": [{"id": 11, "nombre": "Test MM - Clase A",
                    "moneda": {"nombre": "Peso Argentina"},
                    "honorarios": {"administracion_gerente": "0.1", "administracion_depositaria": "0.1"}}],
    }]},
    "matriz": {"fecha_base": "2026-06-04", "generated_at": "2026-06-04T20:00:00-03:00",
               "clases": {"11": {"valor_cuotaparte": "100.0", "fecha_valor": "2026-06-04",
                                 "mes_1": {"tna": "20.0", "directo": "1.6"},
                                 "meses_12": {"tna": "30.0", "directo": "30.0"}}}},
}


class _StubIdx:
    def cer_series(self):
        return {}

    def a3500_series(self):
        return {}


class _StubFx:
    def get_mep_venta(self):
        return 1255.0


def _setup(monkeypatch):
    CAFCIProvider._dataset = _parse_payload(_PAYLOAD)
    CAFCIProvider._disk_loaded = True
    CAFCIProvider._last_attempt = date.today()
    CAFCIProvider._last_fail_ts = 0.0
    monkeypatch.setattr(fci_service, "fetch_ard_fci_rows", lambda: [])
    fci_service.clear_cache()
    app.dependency_overrides[get_indices] = lambda: _StubIdx()
    app.dependency_overrides[get_fx] = lambda: _StubFx()


def _teardown():
    app.dependency_overrides.clear()
    CAFCIProvider._dataset = {"meta": {}, "funds": []}
    CAFCIProvider._disk_loaded = False
    CAFCIProvider._last_attempt = None
    fci_service.clear_cache()


def test_fci_page_loads_client_app():
    with TestClient(app) as c:
        r = c.get("/fci")
    assert r.status_code == 200
    assert "/static/js/fci.js" in r.text and 'id="vtabs"' in r.text


def test_fci_data_returns_dataset(monkeypatch):
    _setup(monkeypatch)
    try:
        with TestClient(app) as c:
            r = c.get("/fci/data")
        assert r.status_code == 200
        data = r.json()
        assert "meta" in data and "funds" in data
        meta = data["meta"]
        for k in ("fecha_base", "cat_order", "subs_by_cat", "hist_axis", "macro",
                  "n_total", "n_shown", "flows_real"):
            assert k in meta
        assert len(data["funds"]) == 1                  # 1 clase → 1 fondo unificado
        f = data["funds"][0]
        assert f["fondo"] == "Test MM" and f["sub"] == "MONEY MARKET ARS CLÁSICO"
        assert "compo" not in f and len(f["flows"]) == 12 and f["flows_real"] is False
        assert f["hist"] is not None                    # reconstruido de los retornos
        assert f["clases"] and f["clases"][0]["cid"] == 11
    finally:
        _teardown()
