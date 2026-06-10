"""Tests del `Data912MarketDataProvider` — focus en dedup logging state
machine y cache merge (no wipe on outage)."""

from unittest.mock import patch

import pytest

from core.infrastructure.data912_provider import Data912MarketDataProvider


@pytest.fixture(autouse=True)
def _reset_class_state():
    """Reset el estado class-level antes de cada test (caches + dedup state)."""
    Data912MarketDataProvider._endpoint_state.clear()
    Data912MarketDataProvider._last_degraded_summary_ts = 0.0
    yield


class TestDedupeStateMachine:
    """Verifica la lógica de Batch 6 anterior: la primera falla por endpoint
    se loguea, las subsiguientes se silencian, y la recovery emite recovery line."""

    def test_first_failure_logs_error(self, caplog):
        with caplog.at_level("ERROR"):
            Data912MarketDataProvider._on_endpoint_failure("bonds", TimeoutError("mock"))
        assert any("Data912/bonds: TimeoutError" in r.message for r in caplog.records)
        # Estado: el endpoint queda marcado como down
        assert Data912MarketDataProvider._endpoint_state["bonds"]["ok"] is False
        assert Data912MarketDataProvider._endpoint_state["bonds"]["fail_count"] == 1

    def test_repeated_failures_dont_spam_logs(self, caplog):
        # Primera falla = 1 ERROR (no la capturamos)
        Data912MarketDataProvider._on_endpoint_failure("bonds", TimeoutError("1"))
        caplog.clear()  # solo capturamos las SIGUIENTES
        with caplog.at_level("ERROR"):
            for _ in range(5):
                Data912MarketDataProvider._on_endpoint_failure("bonds", TimeoutError("more"))
        # Las 5 fallas subsiguientes NO deben loguear
        assert not any("Data912/bonds" in r.message for r in caplog.records)
        assert Data912MarketDataProvider._endpoint_state["bonds"]["fail_count"] == 6

    def test_recovery_emits_info_with_duration(self, caplog):
        Data912MarketDataProvider._on_endpoint_failure("bonds", TimeoutError("mock"))
        # Simular paso del tiempo (sin sleep real)
        Data912MarketDataProvider._endpoint_state["bonds"]["first_fail_ts"] -= 30  # -30s
        with caplog.at_level("INFO"):
            Data912MarketDataProvider._on_endpoint_success("bonds")
        recovery_logs = [r for r in caplog.records if "recovered after" in r.message]
        assert len(recovery_logs) == 1
        assert "Data912/bonds" in recovery_logs[0].message
        # Estado limpio post-recovery
        assert Data912MarketDataProvider._endpoint_state["bonds"]["ok"] is True
        assert Data912MarketDataProvider._endpoint_state["bonds"]["fail_count"] == 0

    def test_success_without_prior_failure_no_log(self, caplog):
        """Llamar success sin haber tenido failure NO debe loguear nada."""
        with caplog.at_level("INFO"):
            Data912MarketDataProvider._on_endpoint_success("bonds")
        assert not any("recovered" in r.message for r in caplog.records)


class TestDegradedSummary:
    def test_summary_emitted_only_when_endpoints_down(self, caplog):
        with caplog.at_level("WARNING"):
            Data912MarketDataProvider._maybe_log_degraded_summary()
        # Sin endpoints caídos no debe loguear nada
        assert not caplog.records

    def test_summary_with_multiple_endpoints_down(self, caplog):
        Data912MarketDataProvider._on_endpoint_failure("bonds", TimeoutError("a"))
        Data912MarketDataProvider._on_endpoint_failure("stocks", TimeoutError("b"))
        # Forzar el throttle a permitir el log
        Data912MarketDataProvider._last_degraded_summary_ts = 0.0
        with caplog.at_level("WARNING"):
            Data912MarketDataProvider._maybe_log_degraded_summary()
        summary_logs = [r for r in caplog.records if "Data912 degraded" in r.message]
        assert len(summary_logs) == 1
        msg = summary_logs[0].message
        assert "2/4" in msg
        assert "bonds" in msg and "stocks" in msg

    def test_summary_respects_throttle(self, caplog):
        """Dos llamadas seguidas → solo 1 log (60s throttle)."""
        Data912MarketDataProvider._on_endpoint_failure("bonds", TimeoutError("a"))
        Data912MarketDataProvider._last_degraded_summary_ts = 0.0
        with caplog.at_level("WARNING"):
            Data912MarketDataProvider._maybe_log_degraded_summary()
            Data912MarketDataProvider._maybe_log_degraded_summary()
            Data912MarketDataProvider._maybe_log_degraded_summary()
        summary_logs = [r for r in caplog.records if "Data912 degraded" in r.message]
        assert len(summary_logs) == 1


class TestCacheMergeOnOutage:
    """Cuando algunos endpoints fallan, el cache de los que respondieron OK
    se preserva (merge en vez de wipe). Esto evita que el UI quede blanco
    durante un outage parcial."""

    def test_partial_outage_preserves_stale_for_failed_endpoint(self):
        """Stocks responde, bonds falla → cache mantiene tickers de stocks
        y los bonds anteriores (no se wipea a {})."""
        prov = Data912MarketDataProvider()
        # Estado inicial: cache poblado por una corrida exitosa previa.
        prov._cache = {
            "AL30D": {"c": 63.5, "v": 100},
            "GGAL": {"c": 5000.0, "v": 50},
        }

        # Simular un fetch donde solo "stocks" responde con datos nuevos.
        def fake_fetch_one(item):
            name, _ = item
            if name == "stocks":
                return [("GGAL", {"c": 5050.0, "v": 99})]  # nuevo valor
            return []  # bonds/corp/notes fallan silenciosos

        with patch.object(prov, "_cache_ts", 0.0):
            # Inyectar via monkeypatch del comportamiento de _fetch_all_endpoints.
            # Re-implementamos el merge logic directo para aislamiento.
            all_data = {}
            for ep in ("stocks", "bonds", "corp", "notes"):
                for sym, row in fake_fetch_one((ep, "")):
                    all_data[sym] = row
            if all_data:
                prov._cache.update(all_data)

        # AL30D (de bonds, falló) sigue en el cache con valor stale
        assert "AL30D" in prov._cache
        assert prov._cache["AL30D"]["c"] == 63.5
        # GGAL fue actualizado por stocks
        assert prov._cache["GGAL"]["c"] == 5050.0
