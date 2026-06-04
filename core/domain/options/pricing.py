"""Pricing CRR binomial americano + solver de volatilidad implícita.

Las opciones argentinas son **americanas** (early exercise posible). Black-Scholes
modela solo europeas → no alcanza. Implementamos Cox-Ross-Rubinstein:

  - Árbol binomial recombinante con N pasos (default 100).
  - dt = T/N, u = exp(σ√dt), d = 1/u, p = (exp((r-q)·dt) − d) / (u − d).
  - Backward induction: en cada nodo el valor es max(intrinsic, discounted_expectation).

IV implícita: Brent's method (scipy.optimize.brentq) sobre f(σ) = CRR(σ) − price_market.
Bracket [0.01, 5.0]. Si el precio queda fuera del rango legal [intrinsic, S] devuelve None.

Performance: una opción ~1-2ms con N=100. Para chains de ~30 strikes ~30ms. La capa
superior (`chain.py`) vectoriza llamando a este módulo para cada contrato (no se
amortiza un árbol compartido porque cada strike requiere su propio backward pass).
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np


def _validate(S: float, K: float, T: float, sigma: float) -> bool:
    return S > 0 and K > 0 and T > 0 and sigma > 0


def crr_american(S: float, K: float, r: float, q: float, sigma: float,
                 T: float, kind: str, N: int = 100) -> float:
    """Precio CRR americano. `kind` ∈ {"C", "V"} (V = put en notación BYMA).

    S: spot, K: strike, r: tasa libre de riesgo cont. comp., q: dividend yield,
    sigma: volatilidad anualizada, T: tiempo a vto (años), N: pasos del árbol.
    """
    if T <= 0 or sigma <= 0:
        # Limit: valor intrínseco.
        return max(0.0, S - K) if kind == "C" else max(0.0, K - S)
    if not _validate(S, K, T, sigma):
        return 0.0
    dt = T / N
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    disc = math.exp(-r * dt)
    p = (math.exp((r - q) * dt) - d) / (u - d)
    if p <= 0 or p >= 1:
        # Parámetros patológicos (σ muy baja con r alto): degenera.
        return max(0.0, S - K) if kind == "C" else max(0.0, K - S)

    # Stock prices at maturity (vector de N+1).
    j = np.arange(N + 1)
    ST = S * (u ** (N - j)) * (d ** j)
    # Payoff terminal.
    if kind == "C":
        V = np.maximum(ST - K, 0.0)
    else:
        V = np.maximum(K - ST, 0.0)
    # Backward induction con early-exercise check en cada nodo.
    for i in range(N - 1, -1, -1):
        ST = ST[:-1] / u  # nivel i: precios del subyacente en cada nodo
        cont = disc * (p * V[:-1] + (1.0 - p) * V[1:])
        intrinsic = np.maximum(ST - K, 0.0) if kind == "C" else np.maximum(K - ST, 0.0)
        V = np.maximum(cont, intrinsic)
    return float(V[0])


def iv_implied(price: float, S: float, K: float, r: float, q: float,
               T: float, kind: str, N: int = 80) -> Optional[float]:
    """Resuelve σ tal que CRR(σ) = price. None si no converge / precio fuera de rango.

    Usa bisección + Newton hybrid simple (suficientemente robusto para los rangos
    típicos AR: σ ∈ [0.05, 3.0]). Para chains masivas (~1050 opciones) el costo
    total es ~5s con N=80 — aceptable en el refresh loop async (corre en to_thread).
    """
    if price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None
    intrinsic = max(0.0, S - K) if kind == "C" else max(0.0, K - S)
    # No puede valer menos que el intrínseco (americana) ni más que S (call) o K (put).
    cap = S if kind == "C" else K
    if price < intrinsic - 1e-4 or price > cap + 1e-2:
        return None

    lo, hi = 1e-3, 5.0
    # Bisección clásica; suficiente para precisión ~1e-4 en ~50 iteraciones.
    p_lo = crr_american(S, K, r, q, lo, T, kind, N=N)
    p_hi = crr_american(S, K, r, q, hi, T, kind, N=N)
    if (p_lo - price) * (p_hi - price) > 0:
        # Sin cambio de signo en el bracket → fuera de rango.
        return None
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        p_mid = crr_american(S, K, r, q, mid, T, kind, N=N)
        if abs(p_mid - price) < 1e-4:
            return mid
        if (p_lo - price) * (p_mid - price) < 0:
            hi = mid
            p_hi = p_mid
        else:
            lo = mid
            p_lo = p_mid
        if hi - lo < 1e-5:
            break
    return 0.5 * (lo + hi)
