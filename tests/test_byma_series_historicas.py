"""Series históricas de BYMA open (core.infrastructure.byma.series_historicas):
paginación por ventanas, parseo de precioCierre, fallback de plazo (24hs→CI),
manejo de 'wheel' (ventana grande) y del fondo de historia ('years'). Sin red:
un cliente fake simula el endpoint /seriesHistoricas/especies."""

from datetime import date, timedelta

from core.infrastructure.byma import series_historicas as sh


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeByma:
    """Server BYMA fake. `truth` = {maturity: {date: close}}; devuelve las filas con
    fecha en [fromDate, toDate]. Opcional: `floor` (toDate < floor → error 'years')
    y `wheel_days` (ventana > N días → error 'wheel', fuerza el halving del cliente)."""

    def __init__(self, truth, *, floor=None, wheel_days=None, status=200):
        self.truth = truth
        self.floor = floor
        self.wheel_days = wheel_days
        self.status = status
        self.calls = 0

    def post(self, url, json=None, headers=None):
        self.calls += 1
        if self.status != 200:
            return _Resp(self.status, {"message": "boom"})
        body = json
        frm = date.fromisoformat(body["fromDate"])
        to = date.fromisoformat(body["toDate"])
        if self.wheel_days is not None and (to - frm).days > self.wheel_days:
            return _Resp(400, {"message": "wheel out of range"})
        if self.floor is not None and to < self.floor:
            return _Resp(400, {"message": "internal_historical_series_years"})
        series = self.truth.get(body["maturity"], {})
        rows = [{"fecha": d.isoformat(), "precioCierre": c}
                for d, c in series.items() if frm <= d <= to]
        return _Resp(200, {"data": rows})


def test_fetch_history_paginates_and_parses():
    today = date.today()
    truth = {"2": {today - timedelta(days=1): 100.0,
                   today - timedelta(days=2): 101.0,
                   today - timedelta(days=30): 110.0}}   # 2ª ventana
    fake = _FakeByma(truth)
    out = sh.fetch_history("BPB7C", max_days=400, client=fake)
    assert out == {today - timedelta(days=1): 100.0,
                   today - timedelta(days=2): 101.0,
                   today - timedelta(days=30): 110.0}
    assert fake.calls >= 2                                # paginó ≥ 2 ventanas


def test_fetch_history_maturity_fallback_to_ci():
    today = date.today()
    truth = {"1": {today - timedelta(days=1): 55.0}}      # solo CI (maturity 1)
    fake = _FakeByma(truth)
    out = sh.fetch_history("LETRA", max_days=120, client=fake)
    assert out == {today - timedelta(days=1): 55.0}       # cayó a CI tras 24hs vacío


def test_fetch_history_handles_wheel_halving():
    today = date.today()
    truth = {"2": {today - timedelta(days=1): 98.0}}
    fake = _FakeByma(truth, wheel_days=20)                # ventana de 25d → 'wheel'
    out = sh.fetch_history("BPC7C", max_days=200, client=fake)
    assert out == {today - timedelta(days=1): 98.0}       # el halving no pierde datos


def test_fetch_history_stops_at_years_floor():
    today = date.today()
    truth = {"2": {today - timedelta(days=1): 98.0,
                   today - timedelta(days=2): 97.0}}
    fake = _FakeByma(truth, floor=today - timedelta(days=10))
    out = sh.fetch_history("BPA7C", max_days=400, client=fake)
    assert out == {today - timedelta(days=1): 98.0, today - timedelta(days=2): 97.0}
    assert fake.calls <= 3                                # frenó en el floor, no hasta 400d


def test_fetch_history_best_effort_on_http_error():
    fake = _FakeByma({}, status=500)
    assert sh.fetch_history("BPB7C", client=fake) == {}   # error → {} (no rompe)


def test_fetch_history_empty_symbol():
    assert sh.fetch_history("", client=_FakeByma({})) == {}


def test_fetch_history_skips_malformed_rows():
    """Filas sin fecha / con precioCierre None o no-numérico se saltean; las válidas
    de la misma ventana se conservan."""
    today = date.today()

    class _MalformedClient:
        def post(self, url, json=None, headers=None):
            d = (today - timedelta(days=1)).isoformat()
            return _Resp(200, {"data": [
                {"fecha": d, "precioCierre": 98.0},      # válida
                {"fecha": "", "precioCierre": 1.0},       # sin fecha → skip
                {"fecha": (today - timedelta(days=2)).isoformat(), "precioCierre": None},  # None
                {"fecha": (today - timedelta(days=3)).isoformat(), "precioCierre": "x"},   # no num
            ]})

    out = sh.fetch_history("BPB7C", max_days=30, client=_MalformedClient())
    assert out == {today - timedelta(days=1): 98.0}


def test_fetch_history_200_without_data_key():
    class _NoData:
        def post(self, url, json=None, headers=None):
            return _Resp(200, {"ok": True})               # 200 sin 'data'
    assert sh.fetch_history("BPB7C", client=_NoData()) == {}


def test_fetch_history_transport_error_keeps_already_fetched_windows():
    """Si una ventana N>1 falla por error de transporte, las ventanas YA bajadas NO
    se descartan (best-effort: _post atrapa la excepción y corta conservando lo que hay)."""
    today = date.today()
    good_day = today - timedelta(days=1)

    class _FlakyClient:
        def __init__(self):
            self.n = 0

        def post(self, url, json=None, headers=None):
            self.n += 1
            if self.n == 1:                               # 1ª ventana: dato bueno
                return _Resp(200, {"data": [{"fecha": good_day.isoformat(),
                                             "precioCierre": 98.0}]})
            raise RuntimeError("connection reset")        # 2ª ventana: cae la red

    out = sh.fetch_history("BPB7C", max_days=400, client=_FlakyClient())
    assert out == {good_day: 98.0}                        # conservó la 1ª ventana
