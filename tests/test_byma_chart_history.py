"""Backfill de cierres vía el endpoint chart de BYMA open
(core.infrastructure.byma.chart_history): parseo OHLCV {s,t[],c[]} → {date: close},
sufijo ' 24HS', filtrado de status/valores, best-effort. Sin red (cliente fake .get)."""

from datetime import date, timedelta

from core.infrastructure.byma import chart_history as ch


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeChart:
    """Server chart fake. Captura el último `params` para verificar el símbolo."""

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.last_params = None
        self.calls = 0

    def get(self, url, params=None, headers=None):
        self.calls += 1
        self.last_params = params
        return _Resp(self.status, self.payload)


def test_parses_ohlcv_and_adds_24hs_suffix():
    d1, d2 = date.today() - timedelta(days=1), date.today() - timedelta(days=2)
    payload = {"s": "ok", "t": [ch._unix(d2), ch._unix(d1)],
               "o": [10, 11], "h": [12, 13], "l": [9, 10], "c": [100.5, 101.0],
               "v": [1000, 2000]}
    fake = _FakeChart(payload)
    out = ch.fetch_history("AL30D", max_days=400, client=fake)
    assert out == {d2: 100.5, d1: 101.0}
    assert fake.calls == 1                              # UNA sola llamada (rango entero)
    assert fake.last_params["symbol"] == "AL30D 24HS"  # sufijo agregado


def test_does_not_double_suffix():
    fake = _FakeChart({"s": "ok", "t": [], "c": []})
    ch.fetch_history("GD30C 24HS", client=fake)
    assert fake.last_params["symbol"] == "GD30C 24HS"


def test_skips_non_ok_status():
    fake = _FakeChart({"s": "no_data", "t": [1], "c": [100.0]})
    assert ch.fetch_history("XXXX", client=fake) == {}


def test_skips_nonpositive_and_none_closes():
    d1, d2, d3 = (date.today() - timedelta(days=i) for i in (1, 2, 3))
    payload = {"s": "ok", "t": [ch._unix(d1), ch._unix(d2), ch._unix(d3)],
               "c": [98.0, 0.0, None]}                  # 0 y None se descartan
    out = ch.fetch_history("AL30", client=_FakeChart(payload))
    assert out == {d1: 98.0}


def test_http_error_is_best_effort():
    assert ch.fetch_history("AL30", client=_FakeChart({}, status=500)) == {}


def test_empty_symbol():
    assert ch.fetch_history("", client=_FakeChart({"s": "ok", "t": [], "c": []})) == {}


def test_transport_error_returns_empty():
    class _Boom:
        def get(self, url, params=None, headers=None):
            raise RuntimeError("connection reset")
    assert ch.fetch_history("AL30", client=_Boom()) == {}


def test_index_history_uses_index_url_and_no_24hs_suffix():
    d = date.today() - timedelta(days=1)
    fake = _FakeChart({"s": "ok", "t": [ch._unix(d)], "c": [3084616.64]})
    out = ch.fetch_index_history("M", client=fake)
    assert out == {d: 3084616.64}
    assert fake.last_params["symbol"] == "M"            # código crudo, sin ' 24HS'
