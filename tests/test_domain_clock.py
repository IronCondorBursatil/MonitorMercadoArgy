"""F1 (review): clock inyectable del dominio (core/domain/clock.py).

El pricing core usaba date.today() hardcodeado (tamar.avg_tamar_tna,
tamar.project_cer_at, cashflow_synth), así que los tests de equivalencia seguían
siendo date-dependent aunque el settle estuviera congelado (M0.1 incompleto): la
ventana de promedio TAMAR y la extrapolación CER derivaban con el reloj real.

clock.today() respeta MONITOR_AS_OF (ISO) y por defecto es date.today() — en
producción el comportamiento es idéntico."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.domain.clock import today as clock_today


def test_clock_defaults_to_real_today(monkeypatch):
    monkeypatch.delenv("MONITOR_AS_OF", raising=False)
    assert clock_today() == date.today()


def test_clock_respects_as_of_env(monkeypatch):
    monkeypatch.setenv("MONITOR_AS_OF", "2026-06-10")
    assert clock_today() == date(2026, 6, 10)


def test_clock_rejects_garbage_env(monkeypatch):
    monkeypatch.setenv("MONITOR_AS_OF", "not-a-date")
    with pytest.raises(ValueError):
        clock_today()


# --- el pricing core debe usar el clock ------------------------------------- #

class _TamarProvider:
    """get_tamar conocido (10% TNA) para toda fecha; sin serie cacheada."""
    def get_tamar(self, d):
        return 10.0


def test_avg_tamar_tna_respects_as_of(monkeypatch):
    """La porción 'observada' debe cortarse en AS_OF, no en el today real: con
    AS_OF=X y período [X-2, X+2], past son 3 días (10%/año) y future 2 días al
    forecast (50%) → avg = (0.3 + 1.0) / 5 = 0.26. Si usara today real (muy
    posterior), past serían los 5 días → 0.1."""
    import core.domain.pricing.tamar as tamar

    as_of = date(2026, 6, 10)
    monkeypatch.setenv("MONITOR_AS_OF", as_of.isoformat())
    with tamar._AVG_TAMAR_LOCK:
        tamar._AVG_TAMAR_CACHE.clear()
        tamar._AVG_TAMAR_DAY = None

    avg = tamar.avg_tamar_tna(as_of - timedelta(days=2), as_of + timedelta(days=2),
                              _TamarProvider(), forecast_tna=0.50)
    assert avg == pytest.approx(0.26, abs=1e-9), \
        "la ventana past debe terminar en AS_OF (clock), no en date.today()"


def test_project_cer_at_respects_as_of(monkeypatch):
    """La extrapolación CER debe anclar en cer(AS_OF) y growth cer(AS_OF)/cer(AS_OF-30),
    no en el today real."""
    import core.domain.pricing.tamar as tamar

    as_of = date(2026, 6, 10)
    monkeypatch.setenv("MONITOR_AS_OF", as_of.isoformat())

    class _Cer:
        def get_cer(self, d):
            if d == as_of:
                return 1000.0
            if d == as_of - timedelta(days=30):
                return 990.0
            return None   # cualquier otra fecha (p.ej. today real) → no hay dato

    target = as_of + timedelta(days=30)   # 1 mes adelante → 1000 × (1000/990)
    out = tamar.project_cer_at(target, _Cer())
    assert out == pytest.approx(1000.0 * (1000.0 / 990.0), rel=1e-9)
