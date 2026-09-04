"""W1 (dominio) — Fase 4, item 4: parametro `seed` (warm-start) de `_xirr_from_years`.

El solver acepta el PRIMER arranque que converge a `|NPV| < 1e-4` (xirr.py), asi que
un seed distinto puede devolver un `r` distinto en los ultimos digitos. Este archivo
es la MEDICION de ese riesgo, no una celebracion del feature:

  `test_riesgo_warm_start_no_mueve_la_tir` corre TODO el catalogo real x 3 precios
  comparando la TIR sin seed contra la TIR con `seed = resultado previo`, y exige
  |a-b| <= 1e-9*max(1,|a|). Si un dia deja de cumplirse, el warm-start NO se cablea.

El cableado (PricingContext / strategies / generate_report) es de la ola 2. Aca solo
vive el parametro y su logica; con `seed=None` el comportamiento es IDENTICO al
historico, y eso tambien se testea (`test_seed_none_es_bit_identico_al_solver_viejo`).
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from core.domain import xirr as X

SETTLE = date(2026, 6, 10) + timedelta(days=1)
TOL_REL = 1e-9


def _solver_original(flows, years):
    """El camino ORIGINAL, sin atajo cerrado y sin seed."""
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


# --------------------------------------------------------------------------- #
# seed=None ⇒ camino historico intacto
# --------------------------------------------------------------------------- #

def test_seed_none_no_construye_guesses(monkeypatch):
    """Con `seed=None` el solver tiene que iterar la MISMA tupla de siempre (sin
    copiarla ni anteponer nada): es el camino que corre el 100% de la app hoy."""
    vistos = []
    orig = X.newton
    monkeypatch.setattr(X, "newton",
                        lambda f, g, **k: (vistos.append(g), orig(f, g, **k))[1])
    X._xirr_from_years(np.array([-100.0, 5.0, 105.0]), np.array([0.0, 1.0, 2.0]))
    assert vistos[0] == X._XIRR_GUESSES[0]


def test_seed_none_es_bit_identico_al_solver_viejo(casos_multiflujo):
    """Multi-flujo (donde el atajo cerrado NO aplica): sin seed, el resultado tiene
    que ser BIT-IDENTICO al solver original. Igualdad `==`, no `approx`."""
    for ticker, flows, years in casos_multiflujo:
        f, y = np.array(flows), np.array(years)
        nuevo = X._xirr_from_years(f, y)
        viejo = _solver_original(f, y)
        if np.isnan(nuevo) and np.isnan(viejo):
            continue
        assert nuevo == viejo, "seed=None movio la TIR de %s: %r vs %r" % (
            ticker, nuevo, viejo)


# --------------------------------------------------------------------------- #
# El seed se usa de verdad y se somete al mismo criterio de aceptacion
# --------------------------------------------------------------------------- #

def test_seed_finito_es_el_primer_arranque(monkeypatch):
    vistos = []
    orig = X.newton
    monkeypatch.setattr(X, "newton",
                        lambda f, g, **k: (vistos.append(g), orig(f, g, **k))[1])
    X._xirr_from_years(np.array([-100.0, 5.0, 105.0]), np.array([0.0, 1.0, 2.0]),
                       seed=0.0731)
    assert vistos[0] == 0.0731, "el seed no se probo primero: %r" % vistos[:2]


@pytest.mark.parametrize("seed", [None, float("nan"), float("inf"), -1.0, -2.5,
                                  object(), [0.07], "no-numerico"])
def test_seed_invalido_cae_a_los_guesses_de_siempre(monkeypatch, seed):
    """Un seed no finito, <= -1 (polo del descuento) o no numerico se IGNORA:
    el solver arranca por `_XIRR_GUESSES` como siempre, sin lanzar."""
    vistos = []
    orig = X.newton
    monkeypatch.setattr(X, "newton",
                        lambda f, g, **k: (vistos.append(g), orig(f, g, **k))[1])
    r = X._xirr_from_years(np.array([-100.0, 5.0, 105.0]), np.array([0.0, 1.0, 2.0]),
                           seed=seed)
    assert vistos[0] == X._XIRR_GUESSES[0], "seed invalido %r se colo" % (seed,)
    assert np.isfinite(r)


def test_seed_que_no_converge_no_rompe_el_resultado():
    """Un seed malo (TIR vieja de otro bono) no puede degradar el resultado: si no
    pasa el criterio de aceptacion, el solver sigue con los guesses y con brentq."""
    f = np.array([-100.0, 5.0, 105.0])
    y = np.array([0.0, 1.0, 2.0])
    base = X._xirr_from_years(f, y)
    for malo in (-0.999999, 5000.0, 1e17):
        r = X._xirr_from_years(f, y, seed=malo)
        assert abs(X._npv(f, y, r)) < X._XIRR_TOLERANCE
        assert abs(r - base) <= TOL_REL * max(1.0, abs(base))


# --------------------------------------------------------------------------- #
# LA MEDICION DE RIESGO (la razon de ser de este archivo)
# --------------------------------------------------------------------------- #

def test_riesgo_warm_start_no_mueve_la_tir(casos_catalogo):
    """TODO el catalogo real x 3 precios: TIR sin seed vs TIR con seed = resultado
    previo (el warm-start real: la TIR del ciclo anterior del mismo bono).

    Si esto falla, el warm-start NO se cablea: no vale mover numeros que se miran
    para operar a cambio de milisegundos."""
    peor = 0.0
    peor_caso = None
    movidos = 0
    comparados = 0
    for ticker, series in casos_catalogo:
        previa = None
        for flows, years in series:
            f, y = np.array(flows), np.array(years)
            sin_seed = X._xirr_from_years(f, y)
            con_seed = X._xirr_from_years(f, y, seed=previa)
            previa = sin_seed if np.isfinite(sin_seed) else None
            if np.isnan(sin_seed) and np.isnan(con_seed):
                continue
            assert np.isfinite(sin_seed) == np.isfinite(con_seed), (
                "el seed cambio la CONVERGENCIA de %s: %r vs %r" % (
                    ticker, sin_seed, con_seed))
            comparados += 1
            rel = abs(sin_seed - con_seed) / max(1.0, abs(sin_seed))
            if rel > 0:
                movidos += 1
            if rel > peor:
                peor, peor_caso = rel, (ticker, sin_seed, con_seed)
    print("\n  comparaciones=%d  con dif != 0: %d  max rel = %.3e  %s"
          % (comparados, movidos, peor, peor_caso))
    # NOTA: adentro de pytest el catalogo esta SANDBOXEADO (conftest redirige
    # `db_dir` a %TEMP% y siembra del Excel master) => ~80 bonos. La medicion sobre
    # los 1158 del catalogo VIVO corre aparte, contra una COPIA read-only.
    assert comparados > 300, "muestra insuficiente: %d" % comparados
    assert peor <= TOL_REL, (
        "WARM-START RECHAZADO: mueve la TIR %.3e (>%g) en %r" % (peor, TOL_REL, peor_caso))


# --------------------------------------------------------------------------- #
# Fixtures — flujos REALES del catalogo
# --------------------------------------------------------------------------- #

def _build(settle=SETTLE):
    from core.domain.pricing import metrics
    from core.infrastructure.db.catalog_repository import CatalogRepository

    casos = []
    for inst in CatalogRepository().get_all_instruments():
        fut, yfs = metrics.discount_year_fractions(inst, settle)
        if not fut or any(y <= 0 for y in yfs):
            continue
        total = sum(cf.total for cf in fut)
        if total <= 0:
            continue
        serie = []
        # precios encadenados como los de ciclos consecutivos (movimientos chicos)
        for f in (0.90, 0.92, 0.925, 0.94, 1.02):
            serie.append(([-total * f] + [cf.total for cf in fut], [0.0] + list(yfs)))
        casos.append((inst.ticker, serie))
    return casos


@pytest.fixture(scope="module")
def casos_catalogo():
    casos = _build()
    assert len(casos) > 60, "el catalogo no trajo instrumentos: %d" % len(casos)
    return casos


@pytest.fixture(scope="module")
def casos_multiflujo(casos_catalogo):
    """Solo los que NO caen en el atajo cerrado (mas de un flujo futuro)."""
    out = [(t, flows, years) for t, serie in casos_catalogo for flows, years in serie
           if len(flows) > 2]
    assert len(out) > 150, "muestra multi-flujo insuficiente: %d" % len(out)
    return out
