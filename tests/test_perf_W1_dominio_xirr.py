"""W1 (dominio) — Fase 4: XIRR cerrado para mono-flujo + seed opcional de warm-start.

ITEM 3 — cerrado mono-flujo. Un stream de DOS flujos (`-precio` hoy, `pago` a t) tiene
IRR unica y CERRADA: `r = (pago/precio)^(1/t) - 1`. Hoy esas ~100 patas (LECAP / LECER /
BONCER ZC / cualquier bono al que le quede un solo flujo) pasan por el root-finder
(secante x5 seeds + brentq de respaldo). Va EN EL SOLVER, aguas abajo de todas las
strategies, para no duplicarlo por tipo.

  OJO: el camino especial LECAP de `pricing/base.py:47-63` es V.Tecnico, NO TIR.

ITEM 4 — `seed` opcional. Ver `tests/test_perf_W1_dominio_xirr_seed.py`.
"""

from __future__ import annotations

import numpy as np
import pytest

from core.domain import xirr as X


def _npv(flows, years, r):
    return float((np.asarray(flows) / (1.0 + r) ** np.asarray(years)).sum())


# --------------------------------------------------------------------------- #
# El atajo existe y NO pasa por el root-finder
# --------------------------------------------------------------------------- #

@pytest.fixture
def sin_root_finder(monkeypatch):
    """Desarma el root-finder: si el atajo cerrado no actua, el test revienta."""
    def _boom(*a, **k):
        raise AssertionError("se llamo al root-finder para un caso mono-flujo")
    monkeypatch.setattr(X, "newton", _boom)
    monkeypatch.setattr(X, "brentq", _boom)


@pytest.mark.parametrize("payoff,px,t", [
    (132.05, 100.0, 1.0),        # LECAP tipica
    (100.0, 95.0, 0.25),         # LECER corta
    (100.0, 100.0, 2.0),         # a la par -> r = 0 exacto
    (150.0, 40.0, 3.5),          # yield alto
    (100.0, 130.0, 0.75),        # yield negativo
    (100.0, 99.999, 0.002740),   # a un dia del vencimiento
])
def test_mono_flujo_resuelve_cerrado(sin_root_finder, payoff, px, t):
    r = X._xirr_from_years(np.array([-px, payoff]), np.array([0.0, t]))
    esperado = (payoff / px) ** (1.0 / t) - 1.0
    assert r == esperado, "no devolvio la raiz algebraica exacta"
    assert abs(_npv([-px, payoff], [0.0, t], r)) < 1e-9


def test_mono_flujo_con_t0_distinto_de_cero(sin_root_finder):
    """La formula generaliza a `dt = t1 - t0` (no asume que el precio esta en t=0)."""
    r = X._xirr_from_years(np.array([-100.0, 121.0]), np.array([0.5, 2.5]))
    assert r == pytest.approx((121.0 / 100.0) ** (1.0 / 2.0) - 1.0, abs=0)
    assert abs(_npv([-100.0, 121.0], [0.5, 2.5], r)) < 1e-9


# --------------------------------------------------------------------------- #
# GUARDS: fuera del caso de raiz unica, el camino viejo intacto
# --------------------------------------------------------------------------- #

def test_tres_flujos_sigue_por_el_root_finder(monkeypatch):
    llamadas = []
    orig = X.newton
    monkeypatch.setattr(X, "newton", lambda *a, **k: (llamadas.append(1), orig(*a, **k))[1])
    r = X._xirr_from_years(np.array([-100.0, 5.0, 105.0]), np.array([0.0, 1.0, 2.0]))
    assert llamadas, "el atajo se aplico a un stream de 3 flujos"
    assert abs(_npv([-100.0, 5.0, 105.0], [0.0, 1.0, 2.0], r)) < 1e-4


@pytest.mark.parametrize("flows,years", [
    ([100.0, 132.0], [0.0, 1.0]),
    ([-100.0, -132.0], [0.0, 1.0]),
    ([-100.0, 0.0], [0.0, 1.0]),
    ([-100.0, 132.0], [0.0, 0.0]),
    ([-100.0, 132.0], [1.0, 0.5]),
    ([-100.0, np.nan], [0.0, 1.0]),
    ([-100.0, 132.0], [0.0, np.inf]),
    # fuera de la ventana que el bracketing historico alcanzaba (S12J6 a 1 dia del vto)
    ([-50.0, 100.0], [0.0, 1e-8]),        # r astronomico  -> el viejo daba NaN
    ([-150.0, 100.0], [0.0, 1e-8]),       # r ~ -1 + eps   -> el viejo daba NaN
])
def test_closed_form_devuelve_nan_fuera_de_su_dominio(flows, years):
    r = X._closed_form_two_flows(np.array(flows), np.array(years))
    assert not np.isfinite(r), "el atajo devolvio un numero fuera de su dominio"


def test_sin_atajo_el_resultado_de_los_guards_no_cambia():
    """Los casos degenerados siguen dando NaN (comportamiento historico)."""
    assert np.isnan(X._xirr_from_years(np.array([100.0, 132.0]), np.array([0.0, 1.0])))
    assert np.isnan(X._xirr_from_years(np.array([-100.0, -132.0]), np.array([0.0, 1.0])))
    assert np.isnan(X._xirr_from_years(np.array([-100.0, 0.0]), np.array([0.0, 1.0])))


def test_overflow_no_lanza():
    """Ratio gigante con dt diminuto: `ratio**(1/dt)` desborda -> el atajo tiene que
    degradar (NaN/inf) sin OverflowError y dejar decidir al camino viejo."""
    r = X._xirr_from_years(np.array([-1e-12, 1e12]), np.array([0.0, 1e-6]))
    assert isinstance(r, float)          # no lanza


# --------------------------------------------------------------------------- #
# ORACULO: el atajo no mueve la TIR del catalogo real
# --------------------------------------------------------------------------- #

def test_oraculo_catalogo_real_mono_flujo(mono_flujo_casos):
    """Para TODAS las patas mono-flujo del catalogo x 5 precios: la TIR cerrada vs
    la del root-finder original. Tolerancia 1e-9 relativo (el mismo baremo que el
    plan fija para el warm-start). El cerrado es la raiz EXACTA; la diferencia es el
    residuo de la secante, que corta con `|NPV| < 1e-4`."""
    peor = 0.0
    peor_caso = None
    for ticker, flows, years in mono_flujo_casos:
        nuevo = X._xirr_from_years(np.array(flows), np.array(years))
        viejo = _solver_original(np.array(flows), np.array(years))
        assert np.isfinite(nuevo) == np.isfinite(viejo), ticker
        if not np.isfinite(nuevo):
            continue
        rel = abs(nuevo - viejo) / max(1.0, abs(nuevo))
        if rel > peor:
            peor, peor_caso = rel, (ticker, nuevo, viejo)
    assert peor <= 1e-9, "el atajo movio la TIR: %r (rel=%.3e)" % (peor_caso, peor)
    print("\n  max |cerrado - root_finder| relativo = %.3e  (%s)" % (peor, peor_caso))


def _solver_original(flows, years):
    """El camino ORIGINAL (secante x5 seeds + brentq), sin el atajo."""
    def npv(rate):
        return X._npv(flows, years, rate)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        for guess in X._XIRR_GUESSES:
            try:
                r = X.newton(npv, guess, maxiter=50)
            except (RuntimeError, ValueError, OverflowError, FloatingPointError):
                continue
            if np.isfinite(r) and r > -1.0 and abs(npv(r)) < X._XIRR_TOLERANCE:
                return float(r)
        return X._bracket_and_solve(npv)


@pytest.fixture(scope="module")
def mono_flujo_casos():
    """(ticker, flows, years) de cada instrumento del catalogo al que le queda UN
    solo flujo, a 5 precios alrededor del payoff."""
    from datetime import date, timedelta

    from core.domain.pricing import metrics
    from core.infrastructure.db.catalog_repository import CatalogRepository

    settle = date(2026, 6, 10) + timedelta(days=1)
    casos = []
    for inst in CatalogRepository().get_all_instruments():
        fut, yfs = metrics.discount_year_fractions(inst, settle)
        if len(fut) != 1 or fut[0].total <= 0 or yfs[0] <= 0:
            continue
        payoff = fut[0].total
        for f in (0.5, 0.8, 0.95, 1.05, 1.5):
            casos.append((inst.ticker, [-payoff * f, payoff], [0.0, yfs[0]]))
    assert len(casos) > 100, "el catalogo no trajo patas mono-flujo: %d" % len(casos)
    return casos
