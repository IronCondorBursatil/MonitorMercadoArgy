"""Tests del BondTerminalProvider y el endpoint /api/riesgo-pais."""
from unittest.mock import patch

import pytest

class MockResponse:
    def __init__(self, json_data):
        self._json = json_data
    def raise_for_status(self):
        pass
    def json(self):
        return self._json

from fastapi.testclient import TestClient

from core.infrastructure.bondterminal_provider import BondTerminalProvider

_SAMPLE = {
    "asOf": "2026-06-17T19:36:06.143Z",
    "lastDataTickIso": "2026-06-17T16:35:35.063Z",
    "valuationDate": "2026-06-17T19:46:01.836Z",
    "weightedSpreadBps": 448.578,
    "totalOutstandingMillions": 57768.02,
    "includedCount": 6,
    "excludedCount": 0,
    "deltas": {"oneDay": 87.81, "oneWeek": 31.84, "oneMonth": 6.68},
    "ambitoValue": 428,
    "ambitoDate": "2026-06-17T19:26:02.922Z",
    "ambitoDeltas": {"oneDay": -2, "oneWeek": -75, "oneMonth": -115},
    "sparklineData": [{"date": "2026-06-16", "value": 360}],
    "metadata": {"historyAvailable": True, "calculationVersion": "1.1.0", "historySnapshots": 45},
    "bonds": [{"ticker": "AL30D", "spreadBps": 440.0, "weight": 0.35, "status": "included"}],
    "errors": [],
    "dataQuality": "live",
}


class TestBondTerminalProvider:
    def test_parses_all_fields(self):
        prov = BondTerminalProvider()
        with patch("httpx.get", return_value=MockResponse(_SAMPLE)):
            data = prov.get_riesgo_pais()

        assert data is not None
        assert data["valor_bps"] == pytest.approx(448.578)
        assert data["ambito_bps"] == 428
        assert data["fecha"] == "2026-06-17"
        assert data["delta_1d"] == pytest.approx(87.81)
        assert data["delta_1w"] == pytest.approx(31.84)
        assert data["delta_1m"] == pytest.approx(6.68)
        assert data["ambito_delta_1d"] == -2
        assert data["ambito_delta_1w"] == -75
        assert data["ambito_delta_1m"] == -115
        assert data["data_quality"] == "live"
        assert data["included_count"] == 6
        assert len(data["bonds"]) == 1
        assert len(data["sparkline"]) == 1

    def test_caches_within_ttl(self):
        prov = BondTerminalProvider()
        with patch(
            "httpx.get",
            return_value=MockResponse(_SAMPLE),
        ) as mock:
            prov.get_riesgo_pais()
            prov.get_riesgo_pais()
        mock.assert_called_once()

    def test_force_bypasses_cache(self):
        prov = BondTerminalProvider()
        with patch(
            "httpx.get",
            return_value=MockResponse(_SAMPLE),
        ) as mock:
            prov.get_riesgo_pais()
            prov.get_riesgo_pais(force=True)
        assert mock.call_count == 2

    def test_returns_none_on_first_error(self):
        prov = BondTerminalProvider()
        with patch(
            "httpx.get",
            side_effect=RuntimeError("timeout"),
        ):
            data = prov.get_riesgo_pais()
        assert data is None

    def test_returns_stale_on_subsequent_error(self):
        prov = BondTerminalProvider()
        with patch("httpx.get", return_value=MockResponse(_SAMPLE)):
            prov.get_riesgo_pais(force=True)
        with patch(
            "httpx.get",
            side_effect=RuntimeError("timeout"),
        ):
            stale = prov.get_riesgo_pais(force=True)
        assert stale is not None
        assert stale["valor_bps"] == pytest.approx(448.578)

    def test_ignores_invalid_response(self):
        prov = BondTerminalProvider()
        with patch(
            "httpx.get",
            return_value={"error": "not found"},
        ):
            data = prov.get_riesgo_pais()
        assert data is None

    def test_negative_caching_no_reintenta_en_cada_request(self):
        """Con BondTerminal caído, cada request re-entraba a la red (el TTL exigía
        `_cache is not None`, que nunca se poblaba): N requests = N×10s de timeout
        ocupando workers del threadpool. Ahora el intento fallido también se sella."""
        prov = BondTerminalProvider()
        with patch(
            "httpx.get",
            side_effect=RuntimeError("timeout"),
        ) as mock:
            for _ in range(5):
                assert prov.get_riesgo_pais() is None
        mock.assert_called_once()

    def test_http_corre_fuera_del_lock(self):
        """El fetch NO puede sostener el mutex: si lo hiciera, un endpoint colgado
        serializaría a todos los threads detrás de él."""
        prov = BondTerminalProvider()
        held = []

        def _slow(*a, **kw):
            held.append(prov._lock.locked())
            return _SAMPLE

        with patch("httpx.get", side_effect=_slow):
            prov.get_riesgo_pais()
        assert held == [False]

    def test_handles_missing_deltas(self):
        prov = BondTerminalProvider()
        minimal = {**_SAMPLE, "deltas": None, "ambitoDeltas": None}
        with patch("httpx.get", return_value=MockResponse(minimal)):
            data = prov.get_riesgo_pais()
        assert data is not None
        assert data["delta_1d"] is None
        assert data["ambito_delta_1d"] is None


_PARSED = {
    "valor_bps": 448.578, "ambito_bps": 428, "fecha": "2026-06-17",
    "delta_1d": 87.81, "delta_1w": 31.84, "delta_1m": 6.68,
    "ambito_delta_1d": -2, "ambito_delta_1w": -75, "ambito_delta_1m": -115,
    "included_count": 6, "outstanding_millions": 57768.02,
    "bonds": [], "sparkline": [], "data_quality": "live",
    "metadata": {}, "as_of": "2026-06-17T19:36:06.143Z",
}


class TestApiRiesgoPais:
    def setup_method(self):
        import os
        os.environ["MONITOR_DISABLE_LOOPS"] = "1"

    def _app_client(self, bt_stub):
        from apps.web.app import app
        from apps.web.deps import get_bondterminal
        app.dependency_overrides[get_bondterminal] = lambda: bt_stub
        client = TestClient(app, raise_server_exceptions=True)
        return app, client

    def teardown_method(self):
        from apps.web.app import app
        from apps.web.deps import get_bondterminal
        app.dependency_overrides.pop(get_bondterminal, None)

    def test_returns_data(self):
        class _Stub:
            def get_riesgo_pais(self):
                return _PARSED

        app, client = self._app_client(_Stub())
        resp = client.get("/api/riesgo-pais")
        assert resp.status_code == 200
        body = resp.json()
        assert body["valor_bps"] == pytest.approx(448.578)
        assert body["ambito_bps"] == 428
        assert body["data_quality"] == "live"

    def test_returns_503_when_no_data(self):
        class _Stub:
            def get_riesgo_pais(self):
                return None

        app, client = self._app_client(_Stub())
        resp = client.get("/api/riesgo-pais")
        assert resp.status_code == 503
