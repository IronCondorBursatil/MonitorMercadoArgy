"""Auditoría lote E — el cache de bytes gzippeados de /fci/data no puede pisar el guard
de `fci_service` de "NO memoizar un dataset degradado".

`fci_service.get_fci_dataset` deliberadamente NO memoiza el dataset cuando
ArgentinaDatos falló (aum_index vacío) → se reconstruye en la visita siguiente. El
router memoizaba los BYTES bajo `(generated_at, len(funds))`, dos componentes que NO
cambian entre el dataset degradado y el sano: el mismo corte CAFCI y la misma cantidad
de fondos. Como fci.js usa `fetch` (siempre manda accept-encoding: gzip), ese es el
camino de TODOS los usuarios.
"""

from datetime import date

from fastapi.testclient import TestClient

import apps.web.fci_service as fci_service
import apps.web.routers.fci as fci_router
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
_ARD_OK = [{"fondo": "Test MM - Clase A", "patrimonio": 1e9, "ccp": 1e7}]


class _StubIdx:
    def cer_series(self):
        return {}

    def a3500_series(self):
        return {}


class _StubFx:
    def get_mep_venta(self):
        return 1255.0


def _setup(monkeypatch, ard_rows):
    CAFCIProvider._dataset = _parse_payload(_PAYLOAD)
    CAFCIProvider._disk_loaded = True
    CAFCIProvider._last_attempt = date.today()
    CAFCIProvider._last_fail_ts = 0.0
    monkeypatch.setattr(fci_service, "fetch_ard_fci_rows", lambda: list(ard_rows))
    fci_service.clear_cache()
    app.dependency_overrides[get_indices] = lambda: _StubIdx()
    app.dependency_overrides[get_fx] = lambda: _StubFx()


def _teardown():
    app.dependency_overrides.clear()
    CAFCIProvider._dataset = {"meta": {}, "funds": []}
    CAFCIProvider._disk_loaded = False
    CAFCIProvider._last_attempt = None
    fci_service.clear_cache()
    fci_router._GZ["src"] = fci_router._GZ["body"] = None


def test_gzip_cache_does_not_serve_degraded_dataset(monkeypatch):
    """ARD caído en la primera visita (AUM vacío), recuperado en la segunda: la segunda
    respuesta gzippeada tiene que traer el AUM real, no los bytes viejos."""
    _setup(monkeypatch, [])
    try:
        with TestClient(app) as c:
            first = c.get("/fci/data", headers={"accept-encoding": "gzip"})
            assert first.status_code == 200
            assert first.json()["meta"]["n_aum_real"] == 0        # degradado

            monkeypatch.setattr(fci_service, "fetch_ard_fci_rows", lambda: list(_ARD_OK))
            second = c.get("/fci/data", headers={"accept-encoding": "gzip"})
        assert second.status_code == 200
        body = second.json()
        assert body["meta"]["n_aum_real"] == 1, "el router sirvió los bytes degradados"
        assert body["funds"][0]["aum"] == 1e9
    finally:
        _teardown()


def test_gzip_cache_hits_when_dataset_unchanged(monkeypatch):
    """El cache sigue sirviendo: con el dataset memoizado (mismo objeto), el segundo
    request NO recomprime."""
    _setup(monkeypatch, _ARD_OK)
    try:
        calls = []
        real_compress = fci_router.gzip.compress

        def counting(*a, **k):
            calls.append(1)
            return real_compress(*a, **k)

        monkeypatch.setattr(fci_router.gzip, "compress", counting)
        with TestClient(app) as c:
            c.get("/fci/data", headers={"accept-encoding": "gzip"})
            c.get("/fci/data", headers={"accept-encoding": "gzip"})
        assert len(calls) == 1
    finally:
        _teardown()
