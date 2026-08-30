"""B4: verifica que los gates de cache diario de índices BCRA y CAFCI usan
la fecha Argentina (UTC-3), no la fecha local del sistema. Sin red (monkeypatch)."""

from datetime import date, timedelta


# ── indices_provider ────────────────────────────────────────────────────────

def test_indices_gate_respects_ar_today(monkeypatch):
    """El gate '_fetch_all' no re-fetcha si ya intentó HOY en hora AR."""
    import core.infrastructure.indices_provider as mod

    calls = {"n": 0}

    def fake_fetch(variable_id, days):
        calls["n"] += 1
        return {}

    yesterday_ar = date.today() - timedelta(days=1)
    monkeypatch.setattr(mod, "_fetch_series", fake_fetch)
    monkeypatch.setattr(mod, "_ar_today", lambda: yesterday_ar)

    prov = mod.BCRAIndicesProvider()
    prov._last_attempt = yesterday_ar   # ya intentado "hoy" (ayer local = hoy AR)
    prov._fetch_all()
    assert calls["n"] == 0             # gate cerrado: ar_today() == _last_attempt


def test_indices_gate_opens_next_ar_day(monkeypatch):
    """Al rollover del día AR el gate se abre y se dispara el fetch."""
    import core.infrastructure.indices_provider as mod

    calls = {"n": 0}

    def fake_fetch(variable_id, days):
        calls["n"] += 1
        return {}

    today_ar = date.today()
    yesterday_ar = today_ar - timedelta(days=1)
    monkeypatch.setattr(mod, "_fetch_series", fake_fetch)
    monkeypatch.setattr(mod, "_ar_today", lambda: today_ar)

    prov = mod.BCRAIndicesProvider()
    prov._last_attempt = yesterday_ar   # último intento fue ayer AR
    # El lock de clase compartido puede tener _disk_loaded=True en otros tests;
    # forzamos hydrate bypass sin tocar el CSV real.
    prov._disk_loaded = True
    prov._fetch_all()
    assert calls["n"] > 0              # gate abierto: ar_today() != _last_attempt


# ── cafci_provider ───────────────────────────────────────────────────────────

def test_cafci_gate_respects_ar_today(monkeypatch):
    """CAFCIProvider._ensure_loaded no re-fetcha si ya intentó HOY (hora AR)."""
    import core.infrastructure.cafci_provider as mod

    calls = {"n": 0}

    def fake_http(*a, **kw):
        calls["n"] += 1
        raise RuntimeError("no net")

    yesterday_ar = date.today() - timedelta(days=1)
    monkeypatch.setattr(mod, "_ar_today", lambda: yesterday_ar)
    monkeypatch.setattr("httpx.get", fake_http)

    prov = mod.CAFCIProvider()
    prov._disk_loaded = True
    type(prov)._last_attempt = yesterday_ar   # gate cerrado
    type(prov)._dataset = {"meta": {}, "funds": [{"fondo": "x", "clases": []}]}
    prov._ensure_loaded()
    assert calls["n"] == 0             # gate bloqueó el fetch
