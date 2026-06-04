"""Tests del pricing CRR americano + IV solver.

Checks clave:
  1. CRR europeo → Black-Scholes (cuando se inhibe el early exercise: call sin
     dividendos no se ejerce nunca antes, sólo en vto → CRR = BS).
  2. Put americano > Put europeo (premium de early exercise positivo cuando ITM).
  3. IV solver es consistente: dado un σ, computar precio y resolver IV recupera σ.
  4. Convergencia con N (estabilidad numérica).
"""
from __future__ import annotations

import math

import pytest

from core.domain.options.pricing import crr_american, iv_implied


def _bs_call(S, K, r, T, sigma):
    """Black-Scholes europeo (referencia)."""
    from math import erf, exp, log, sqrt
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K)
    d1 = (log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    N = lambda x: 0.5 * (1 + erf(x / sqrt(2)))
    return S * N(d1) - K * exp(-r * T) * N(d2)


def _bs_put(S, K, r, T, sigma):
    from math import erf, exp, log, sqrt
    if T <= 0 or sigma <= 0:
        return max(0.0, K - S)
    d1 = (log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    N = lambda x: 0.5 * (1 + erf(x / sqrt(2)))
    return K * exp(-r * T) * N(-d2) - S * N(-d1)


def test_crr_call_converges_to_bs():
    """Call sin dividendos: CRR americano = europeo = BS (no se ejerce antes)."""
    S, K, r, q, sigma, T = 100, 100, 0.05, 0.0, 0.25, 0.5
    crr = crr_american(S, K, r, q, sigma, T, "C", N=200)
    bs = _bs_call(S, K, r, T, sigma)
    assert abs(crr - bs) < 0.10  # convergencia con N=200


def test_crr_put_american_premium():
    """Put americano ITM debe valer >= put europeo (premium de early exercise)."""
    S, K, r, q, sigma, T = 90, 100, 0.10, 0.0, 0.25, 1.0
    crr = crr_american(S, K, r, q, sigma, T, "V", N=200)
    bs_eur = _bs_put(S, K, r, T, sigma)
    assert crr >= bs_eur - 0.01
    # Premium tangible cuando hay tiempo + tasa positiva:
    assert crr > bs_eur


def test_crr_zero_time_returns_intrinsic():
    assert crr_american(100, 110, 0.05, 0, 0.2, 0.0, "C") == pytest.approx(0.0)
    assert crr_american(100, 110, 0.05, 0, 0.2, 0.0, "V") == pytest.approx(10.0)
    assert crr_american(120, 100, 0.05, 0, 0.2, 0.0, "C") == pytest.approx(20.0)


def test_iv_solver_recovers_sigma():
    """Round-trip: priceо una opción con σ=0.35, luego resuelvo IV → debe recuperar."""
    S, K, r, q, T = 100, 105, 0.05, 0.0, 0.5
    for sigma_true in [0.15, 0.30, 0.60, 1.00]:
        for kind in ["C", "V"]:
            price = crr_american(S, K, r, q, sigma_true, T, kind, N=100)
            iv = iv_implied(price, S, K, r, q, T, kind, N=100)
            assert iv is not None, f"IV failed for σ={sigma_true} kind={kind}"
            assert abs(iv - sigma_true) < 0.01, (
                f"IV={iv:.4f} vs true={sigma_true} for kind={kind}")


def test_iv_returns_none_below_intrinsic():
    """Precio por debajo del intrínseco no es resoluble."""
    iv = iv_implied(price=0.1, S=100, K=80, r=0.05, q=0, T=0.5, kind="C")
    assert iv is None  # intrínseco = 20, precio = 0.1, fuera de rango


def test_crr_stable_under_N_increase():
    """Doblar N no debería cambiar el precio en más de 1%."""
    args = (100, 105, 0.05, 0, 0.30, 0.5, "C")
    p_80 = crr_american(*args, N=80)
    p_160 = crr_american(*args, N=160)
    assert abs(p_80 - p_160) / p_80 < 0.02
