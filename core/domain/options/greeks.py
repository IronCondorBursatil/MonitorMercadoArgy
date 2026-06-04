"""Griegos por diferencias finitas sobre el árbol CRR americano.

Δ, Γ: bump del spot.
Θ: bump de T (1 día).
Vega: bump de σ (1 vol-point).
Rho: bump de r.

Esto es exacto al modelo (no asume Black-Scholes). Costo: ~4-6× un pricing
por opción. Aceptable para chains de ~30 strikes (~150-200ms total con N=80).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.domain.options.pricing import crr_american


@dataclass(frozen=True)
class Greeks:
    delta: Optional[float]
    gamma: Optional[float]
    theta: Optional[float]   # por día
    vega: Optional[float]    # por 1 vol-point (0.01)
    rho: Optional[float]     # por 1 rate-point (0.01)


def compute_greeks(S: float, K: float, r: float, q: float, sigma: float,
                   T: float, kind: str, N: int = 80) -> Greeks:
    """Diferencias finitas centradas (cuando es posible). Devuelve campos None
    si el cómputo no es válido (T≤0, σ inválida)."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return Greeks(None, None, None, None, None)

    h_s = 0.01 * S
    h_v = 0.01           # 1 vol-point (σ + 0.01)
    h_r = 0.01           # 1 rate-point
    h_t = 1.0 / 365.0    # 1 día

    p0 = crr_american(S, K, r, q, sigma, T, kind, N=N)
    p_up = crr_american(S + h_s, K, r, q, sigma, T, kind, N=N)
    p_dn = crr_american(S - h_s, K, r, q, sigma, T, kind, N=N)
    delta = (p_up - p_dn) / (2.0 * h_s)
    gamma = (p_up - 2.0 * p0 + p_dn) / (h_s * h_s)

    p_v = crr_american(S, K, r, q, sigma + h_v, T, kind, N=N)
    vega = (p_v - p0) / 1.0          # por vol-point (h_v=0.01 = 1 punto)

    p_r = p0  # idéntico a p0 (mismos args, sin bump de r/σ): reusar evita una valuación
    p_rp = crr_american(S, K, r + h_r, q, sigma, T, kind, N=N)
    rho = (p_rp - p_r) / 1.0

    # Theta: bump hacia adelante en tiempo (T - h). Si T < h, valor sin sentido.
    if T > h_t:
        p_t = crr_american(S, K, r, q, sigma, T - h_t, kind, N=N)
        theta = (p_t - p0)   # ya es per-día (h_t = 1/365 → diferencia 1 día)
    else:
        theta = None

    return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)
