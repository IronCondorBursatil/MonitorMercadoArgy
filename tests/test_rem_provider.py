"""REM provider: negative-cache (anti-storm) + fallback ArgentinaDatos (schema 2026).

La primaria (workers.dev `ipc_general`) y el fallback (api.argentinadatos.com
`/finanzas/rem/ultimo`) se mockean sin red. El fallback de ARD ahora trae TODOS los
indicadores (IPC/TAMAR/export/...) y dos muestras (todos/top_10): el normalizador
debe quedarse SOLO con IPC nivel general · muestra=todos para no contaminar el sendero.
"""
from datetime import date

import pytest

import core.infrastructure.rem_provider as rp
from core.infrastructure.rem_provider import REMProvider


@pytest.fixture(autouse=True)
def _reset_rem_cache():
    """El cache de REM es class-level (compartido) → reset entre tests."""
    REMProvider._cache_rows = []
    REMProvider._last_fetch_ts = 0.0
    REMProvider._last_fail_ts = 0.0
    yield
    REMProvider._cache_rows = []
    REMProvider._last_fetch_ts = 0.0
    REMProvider._last_fail_ts = 0.0


# --- Muestra real del schema ARD 2026 (recortada): IPC general (todos + top_10),
#     más un par de indicadores que NO deben filtrarse al sendero. ---
_ARD_SAMPLE = [
    {"indicador": "Precios minoristas (IPC nivel general-Nacional; INDEC)",
     "muestra": "todos", "periodo": "May-26", "periodoTipo": "mensual",
     "referencia": "var. % mensual", "mediana": 2.3, "promedio": 2.29},
    {"indicador": "Precios minoristas (IPC nivel general-Nacional; INDEC)",
     "muestra": "todos", "periodo": "Jun-26", "periodoTipo": "mensual",
     "referencia": "var. % mensual", "mediana": 2.1, "promedio": 2.07},
    {"indicador": "Precios minoristas (IPC nivel general-Nacional; INDEC)",
     "muestra": "todos", "periodo": "próx. 12 meses", "periodoTipo": "proximos_12_meses",
     "referencia": "var. % i.a.; may-27", "mediana": 23.3, "promedio": 23.5},
    # top_10: misma serie, OTRA muestra → debe excluirse (no duplicar).
    {"indicador": "Precios minoristas (IPC nivel general-Nacional; INDEC)",
     "muestra": "top_10", "periodo": "May-26", "periodoTipo": "mensual",
     "referencia": "var. % mensual", "mediana": 9.99, "promedio": 9.99},
    # TAMAR mensual: referencia 'var. % mensual' PERO no es IPC → debe excluirse.
    {"indicador": "Tasa de interés (TAMAR)", "muestra": "todos",
     "periodo": "May-26", "periodoTipo": "mensual",
     "referencia": "var. % mensual", "mediana": 30.0, "promedio": 30.0},
]


def _mock_http(monkeypatch, *, primary_ok=False, ard=None, primary_exc=None):
    calls = {"primary": 0, "ard": 0}

    def fake(url, **kw):
        if "facujallia" in url:                       # primaria
            calls["primary"] += 1
            if primary_exc is not None:
                raise primary_exc
            return {"datos": primary_ok or []}
        if "api.argentinadatos.com" in url:           # fallback (host correcto)
            calls["ard"] += 1
            if ard is None:
                raise ConnectionError("getaddrinfo failed")
            return ard
        raise AssertionError(f"URL inesperada: {url}")  # ningún apex pelado

    monkeypatch.setattr(rp, "http_get_json", fake)
    return calls


def test_ard_fallback_only_ipc_general_todos(monkeypatch):
    """Primaria caída → fallback ARD; queda SOLO IPC general muestra=todos."""
    calls = _mock_http(monkeypatch, primary_exc=ConnectionError("dns"), ard=_ARD_SAMPLE)
    prov = REMProvider()

    path = prov.get_monthly_path()
    # Solo los 2 meses de IPC general/todos (NO top_10=9.99, NO TAMAR=30.0).
    assert path == {date(2026, 5, 1): pytest.approx(0.023),
                    date(2026, 6, 1): pytest.approx(0.021)}
    assert 0.0999 not in path.values() and 0.30 not in path.values()

    # próx. 12 meses (todos) → 23.3 % → 0.233
    assert prov.get_next_12m_yoy() == pytest.approx(0.233)
    assert calls["primary"] == 1 and calls["ard"] == 1


def test_ard_fallback_hits_api_subdomain(monkeypatch):
    """El fallback pega a api.argentinadatos.com (no al apex, que da 404)."""
    seen = {}

    def fake(url, **kw):
        if "facujallia" in url:
            raise ConnectionError("dns")
        seen["url"] = url
        return _ARD_SAMPLE

    monkeypatch.setattr(rp, "http_get_json", fake)
    REMProvider().get_monthly_path()
    assert seen["url"].startswith("https://api.argentinadatos.com/")


def test_negative_cache_stops_storm(monkeypatch):
    """Primaria+fallback caen → tras 1 ronda, las llamadas siguientes NO reintentan
    (negative cache) → no inunda con un fetch por instrumento del ciclo de pricing."""
    calls = _mock_http(monkeypatch, primary_exc=ConnectionError("dns"), ard=None)
    prov = REMProvider()

    prov.get_monthly_path()   # ronda 1: primaria + fallback (2 llamadas) → fallan
    prov.get_monthly_path()   # dentro del cooldown → skip
    prov.get_next_12m_yoy()   # dentro del cooldown → skip
    prov.get_for_month(date(2026, 5, 1))

    assert calls["primary"] == 1
    assert calls["ard"] == 1   # NO se multiplicó por cada caller


def test_negative_cache_recovers_after_cooldown(monkeypatch):
    """Pasado el cooldown, vuelve a intentar (y si la primaria responde, cachea)."""
    calls = _mock_http(monkeypatch, primary_exc=ConnectionError("dns"), ard=None)
    prov = REMProvider()
    prov.get_monthly_path()                       # falla, marca _last_fail_ts
    assert calls["primary"] == 1

    # Simular que el cooldown ya pasó.
    REMProvider._last_fail_ts -= (REMProvider.FAIL_COOLDOWN + 1)
    prov.get_monthly_path()                       # reintenta
    assert calls["primary"] == 2


def test_primary_success_is_cached(monkeypatch):
    """Primaria OK → cachea y no toca el fallback ni re-fetchea dentro del TTL."""
    primary_rows = [{"período": "2026-05-31 00:00:00", "referencia": "var. % mensual",
                     "mediana": 2.6, "promedio": 2.6}]
    calls = _mock_http(monkeypatch, primary_ok=primary_rows, ard=_ARD_SAMPLE)
    prov = REMProvider()
    path = prov.get_monthly_path()
    assert path == {date(2026, 5, 31): pytest.approx(0.026)}
    prov.get_monthly_path()                       # dentro del TTL → sin re-fetch
    assert calls["primary"] == 1 and calls["ard"] == 0
