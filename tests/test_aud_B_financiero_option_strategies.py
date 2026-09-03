"""Auditoría lote B — `core/domain/options/strategies.py` no tenía NINGÚN test.

`payoff_curve` / `preset_legs` / `PayoffResult` se publican como API soportada en
`core/domain/options/__init__.py` pero no tienen caller en el repo (el payoff real
corre en el JS de `pages/options.html`). Sin red de seguridad, un error de signo en
`_leg_payoff` (usar `bid` para un long) o en el cruce de break-even sale a
producción sin que nada lo detecte. Estos tests fijan las identidades verificables
a mano de cada payoff.
"""
from __future__ import annotations

from datetime import date

import pytest

from core.domain.options.chain import OptionItem
from core.domain.options.greeks import Greeks
from core.domain.options.models import OptionContract, OptionLeg
from core.domain.options.rates import ImpliedRates
from core.domain.options.strategies import (
    PRESET_NAMES, payoff_curve, preset_legs,
)

_EXP = date(2026, 12, 18)


def _item(kind: str, strike: float, bid: float, ask: float, *, spot: float = 100.0,
          delta=None, gamma=None, theta=None, vega=None) -> OptionItem:
    tk = f"XX{kind}{int(strike)}DI"
    return OptionItem(
        contract=OptionContract(ticker=tk, root="XX", underlying="XX", kind=kind,
                                strike=strike, month=12, month_code="DI", expiry=_EXP),
        spot=spot, bid=bid, ask=ask, last=(bid + ask) / 2, mid=(bid + ask) / 2,
        volume=0.0, open_interest=0.0, pct_change=None, iv=0.4,
        greeks=Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=None),
        rates=ImpliedRates(tna_bruta=None, tea_bruta=None, tna_strike=None),
        t_days=100,
    )


def _by_ticker(*items):
    return {it.ticker: it for it in items}


# ── payoff_curve: identidades verificables a mano ─────────────────────────────

def test_long_call_pierde_la_prima_y_tiene_un_solo_breakeven():
    """Long call K=100 pagando ask=10: pérdida máxima = 10 (la prima) y un único
    break-even en 110 (strike + prima)."""
    it = _item("C", 100.0, bid=9.0, ask=10.0)
    r = payoff_curve([OptionLeg(it.ticker, 1)], _by_ticker(it), spot=100.0)
    assert r.cost == pytest.approx(10.0)          # débito pagado
    assert r.max_loss == pytest.approx(-10.0)
    assert len(r.breakevens) == 1
    assert r.breakevens[0] == pytest.approx(110.0, abs=0.5)
    assert r.max_gain > 0


def test_long_put_breakeven_es_strike_menos_prima():
    it = _item("V", 100.0, bid=4.0, ask=5.0)
    r = payoff_curve([OptionLeg(it.ticker, 1)], _by_ticker(it), spot=100.0)
    assert r.cost == pytest.approx(5.0)
    assert r.max_loss == pytest.approx(-5.0)
    assert len(r.breakevens) == 1
    assert r.breakevens[0] == pytest.approx(95.0, abs=0.5)


def test_short_call_cobra_prima_y_su_ganancia_maxima_es_el_bid():
    """Venta desnuda: el crédito recibido es el `bid` (no el ask) y la ganancia
    tope es ese crédito."""
    it = _item("C", 100.0, bid=9.0, ask=10.0)
    r = payoff_curve([OptionLeg(it.ticker, -1)], _by_ticker(it), spot=100.0)
    assert r.cost == pytest.approx(-9.0)          # crédito (signo negativo)
    assert r.max_gain == pytest.approx(9.0)
    assert r.max_loss < 0
    assert r.breakevens[0] == pytest.approx(109.0, abs=0.5)


def test_bull_call_spread_debito_y_ganancia_acotada():
    """K1=100 comprada a 10, K2=108 vendida a 4 → débito 6, ganancia tope
    (108-100) - 6 = 2, break-even 106."""
    lo = _item("C", 100.0, bid=9.0, ask=10.0)
    hi = _item("C", 108.0, bid=4.0, ask=5.0)
    r = payoff_curve([OptionLeg(lo.ticker, 1), OptionLeg(hi.ticker, -1)],
                     _by_ticker(lo, hi), spot=100.0, lo_factor=0.5, hi_factor=1.6)
    assert r.cost == pytest.approx(6.0)
    assert r.max_loss == pytest.approx(-6.0)
    assert r.max_gain == pytest.approx(2.0, abs=0.2)
    assert len(r.breakevens) == 1
    assert r.breakevens[0] == pytest.approx(106.0, abs=0.5)


def test_straddle_tiene_dos_breakevens_simetricos():
    c = _item("C", 100.0, bid=6.0, ask=7.0)
    v = _item("V", 100.0, bid=4.0, ask=5.0)
    # n_points=101 sobre [50,150] pone el strike EN la grilla: sin eso `max_loss`
    # es el mínimo muestreado, no el vértice exacto (la curva es discreta).
    r = payoff_curve([OptionLeg(c.ticker, 1), OptionLeg(v.ticker, 1)],
                     _by_ticker(c, v), spot=100.0, n_points=101,
                     lo_factor=0.5, hi_factor=1.5)
    assert r.cost == pytest.approx(12.0)          # 7 + 5
    assert r.max_loss == pytest.approx(-12.0)
    assert len(r.breakevens) == 2
    lo_be, hi_be = sorted(r.breakevens)
    assert lo_be == pytest.approx(88.0, abs=0.8)
    assert hi_be == pytest.approx(112.0, abs=0.8)
    assert (100.0 - lo_be) == pytest.approx(hi_be - 100.0, abs=0.8)


def test_breakeven_detectado_aunque_la_grilla_caiga_justo_en_el_cruce():
    """Defecto encontrado al escribir estos tests: el cruce por cero se detectaba
    con `<`/`>` ESTRICTOS en los dos lados, así que cuando un punto de la grilla
    caía exactamente sobre el break-even (payoff == 0) no se contaba ningún cruce
    y `breakevens` salía VACÍA — justo el caso de las grillas redondas."""
    it = _item("C", 100.0, bid=9.0, ask=10.0)
    r = payoff_curve([OptionLeg(it.ticker, 1)], _by_ticker(it), spot=100.0,
                     n_points=101, lo_factor=0.5, hi_factor=1.5)
    assert 110.0 in r.xs and r.ys[r.xs.index(110.0)] == pytest.approx(0.0)
    assert r.breakevens == [pytest.approx(110.0)]


def test_griegos_netos_suman_por_cantidad_signada():
    lo = _item("C", 100.0, bid=9.0, ask=10.0, delta=0.55, gamma=0.02,
               theta=-0.10, vega=0.20)
    hi = _item("C", 108.0, bid=4.0, ask=5.0, delta=0.30, gamma=0.01,
               theta=-0.06, vega=0.15)
    r = payoff_curve([OptionLeg(lo.ticker, 2), OptionLeg(hi.ticker, -1)],
                     _by_ticker(lo, hi), spot=100.0)
    assert r.delta_net == pytest.approx(2 * 0.55 - 0.30)
    assert r.gamma_net == pytest.approx(2 * 0.02 - 0.01)
    assert r.theta_net == pytest.approx(2 * -0.10 - (-0.06))
    assert r.vega_net == pytest.approx(2 * 0.20 - 0.15)


def test_griegos_ausentes_quedan_en_none():
    it = _item("C", 100.0, bid=9.0, ask=10.0)   # sin griegos
    r = payoff_curve([OptionLeg(it.ticker, 1)], _by_ticker(it), spot=100.0)
    assert r.delta_net is None and r.gamma_net is None
    assert r.theta_net is None and r.vega_net is None


def test_legs_desconocidas_y_spot_invalido_no_rompen():
    it = _item("C", 100.0, bid=9.0, ask=10.0)
    vacio = payoff_curve([OptionLeg("NO-EXISTE", 1)], _by_ticker(it), spot=100.0)
    assert vacio.xs == [] and vacio.ys == [] and vacio.breakevens == []
    sin_spot = payoff_curve([OptionLeg(it.ticker, 1)], _by_ticker(it), spot=0.0)
    assert sin_spot.xs == []


# ── preset_legs ───────────────────────────────────────────────────────────────

def _chain() -> list:
    items = []
    for k in range(80, 126, 5):
        items.append(_item("C", float(k), bid=max(0.5, 105 - k), ask=max(1.0, 106 - k)))
        items.append(_item("V", float(k), bid=max(0.5, k - 95), ask=max(1.0, k - 94)))
    return items


@pytest.mark.parametrize("name", PRESET_NAMES)
def test_todo_preset_publicado_construye_legs(name):
    """Todo nombre de `PRESET_NAMES` (el que el router renderiza como botón) tiene
    que tener builder: si no, el botón devuelve [] y BORRA la estrategia armada."""
    legs = preset_legs(name, _chain(), spot=100.0)
    assert legs, f"el preset '{name}' no construyó ninguna leg"
    assert all(isinstance(l, OptionLeg) and l.qty != 0 for l in legs)


def test_preset_desconocido_devuelve_lista_vacia():
    assert preset_legs("no_existe", _chain(), spot=100.0) == []


def test_presets_de_venta_son_short_y_los_de_compra_long():
    assert [l.qty for l in preset_legs("long_call", _chain(), 100.0)] == [1]
    assert [l.qty for l in preset_legs("short_call", _chain(), 100.0)] == [-1]
    assert [l.qty for l in preset_legs("short_put", _chain(), 100.0)] == [-1]
    assert [l.qty for l in preset_legs("covered_call", _chain(), 100.0)] == [-1]


def test_iron_condor_tiene_cuatro_patas_balanceadas():
    legs = preset_legs("iron_condor", _chain(), spot=100.0)
    assert len(legs) == 4
    assert sum(l.qty for l in legs) == 0


def test_preset_sin_strikes_disponibles_no_explota():
    assert preset_legs("straddle", [], spot=100.0) == []
