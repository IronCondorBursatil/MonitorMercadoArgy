"""Yield curve modelling and Break-Even Inflation.

Implements:
- Nelson-Siegel parametric curve         (BCRA NT N°8/2024, Eq. 11)
- Nelson-Siegel-Svensson (NSS) curve     (BCRA NT N°3/2019, Eq. 17)
- Bootstrapping zero-coupon spot rates   (BCRA NT N°3/2019, Eq. 11-16)
- Fisher parity for break-even inflation (NT N°8/2024, Eq. 8)
- Lag-adjusted spot BEI via gamma factor (NT N°8/2024, Eq. A4 / A10)
- Forward BEI between two tenors         (NT N°3/2019, Eq. 10)
- Pair-of-bonds BEI with γ/δ adjustment  (NT N°8/2024, Apéndice Eq. A13)
- Real FX drift from devaluation + BEI   (Fisher applied to FX vs IPC)

Conventions:
- Tenors are in years.
- Rates are decimal fractions (0.30 = 30% annual).
"""

from dataclasses import dataclass
from datetime import date
from typing import List, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import least_squares


@dataclass(frozen=True)
class NelsonSiegelCurve:
    """Nelson-Siegel curve: i(T) = β₀ + β₁·d(γT) + β₂·(d(γT) - e^(-γT))

    where d(x) = (1 - e^(-x)) / x. β₀ is the long-term level, β₁ the short-term
    slope, β₂ the medium-term curvature, γ the decay rate.
    """

    beta0: float
    beta1: float
    beta2: float
    gamma: float

    def __call__(self, t: float) -> float:
        if t <= 0:
            return self.beta0 + self.beta1
        gT = self.gamma * t
        decay = (1.0 - np.exp(-gT)) / gT
        return self.beta0 + self.beta1 * decay + self.beta2 * (decay - np.exp(-gT))

    @classmethod
    def fit(cls, tenors: Sequence[float], yields: Sequence[float]) -> "NelsonSiegelCurve":
        """Calibrate β₀,β₁,β₂,γ via non-linear least squares on observed points."""
        tenors_arr = np.asarray(tenors, dtype=float)
        yields_arr = np.asarray(yields, dtype=float)
        if tenors_arr.size != yields_arr.size:
            raise ValueError(
                f"tenors/yields length mismatch: {tenors_arr.size} vs {yields_arr.size}"
            )
        if tenors_arr.size < 3:
            raise ValueError("Need at least 3 (tenor, yield) points to fit Nelson-Siegel.")

        def residuals(params):
            b0, b1, b2, g = params
            gT = g * tenors_arr
            decay = (1.0 - np.exp(-gT)) / gT
            model = b0 + b1 * decay + b2 * (decay - np.exp(-gT))
            return model - yields_arr

        x0 = [float(yields_arr.mean()), 0.0, 0.0, 1.0]
        bounds = ([-1.0, -2.0, -2.0, 1e-3], [3.0, 2.0, 2.0, 50.0])
        result = least_squares(residuals, x0, bounds=bounds, max_nfev=2000)
        return cls(float(result.x[0]), float(result.x[1]), float(result.x[2]), float(result.x[3]))


@dataclass(frozen=True)
class NelsonSiegelSvenssonCurve:
    """Nelson-Siegel-Svensson curve (NSS), per BCRA NT N°3/2019 Eq. 17.

    i(T) = β₀ + β₁·d(T/τ₁) + β₂·(d(T/τ₁) − e^(−T/τ₁))
                          + β₃·(d(T/τ₂) − e^(−T/τ₂))

    where d(x) = (1 − e^(−x)) / x. NSS adds a second hump term (β₃, τ₂)
    over Nelson-Siegel, capturing curves with two local maxima (typical in
    AR LECAP after BCRA rate cycles).
    """

    beta0: float
    beta1: float
    beta2: float
    beta3: float
    tau1: float
    tau2: float

    @staticmethod
    def _factors(t: float, tau: float) -> tuple:
        x = t / tau
        e = np.exp(-x)
        decay = (1.0 - e) / x if x > 1e-9 else 1.0
        return decay, e

    def __call__(self, t: float) -> float:
        if t <= 0:
            return self.beta0 + self.beta1
        d1, e1 = self._factors(t, self.tau1)
        d2, e2 = self._factors(t, self.tau2)
        return (self.beta0
                + self.beta1 * d1
                + self.beta2 * (d1 - e1)
                + self.beta3 * (d2 - e2))

    @classmethod
    def fit(cls, tenors: Sequence[float], yields: Sequence[float]) -> "NelsonSiegelSvenssonCurve":
        """Calibrate β₀..β₃, τ₁, τ₂ via least squares. Needs ≥4 points."""
        tenors_arr = np.asarray(tenors, dtype=float)
        yields_arr = np.asarray(yields, dtype=float)
        if tenors_arr.size != yields_arr.size:
            raise ValueError(
                f"tenors/yields length mismatch: {tenors_arr.size} vs {yields_arr.size}"
            )
        if tenors_arr.size < 4:
            raise ValueError("Need at least 4 (tenor, yield) points to fit NSS.")

        def residuals(params):
            b0, b1, b2, b3, t1, t2 = params
            x1 = tenors_arr / t1
            x2 = tenors_arr / t2
            e1 = np.exp(-x1)
            e2 = np.exp(-x2)
            d1 = (1.0 - e1) / x1
            d2 = (1.0 - e2) / x2
            model = b0 + b1 * d1 + b2 * (d1 - e1) + b3 * (d2 - e2)
            return model - yields_arr

        # τ₁ < τ₂ keeps roles distinguishable (short-vs-long hump).
        x0 = [float(yields_arr.mean()), 0.0, 0.0, 0.0, 0.5, 3.0]
        bounds = ([-1.0, -2.0, -2.0, -2.0, 1e-2, 1e-2],
                  [ 3.0,  2.0,  2.0,  2.0, 30.0, 60.0])
        result = least_squares(residuals, x0, bounds=bounds, max_nfev=4000)
        return cls(
            float(result.x[0]), float(result.x[1]), float(result.x[2]),
            float(result.x[3]), float(result.x[4]), float(result.x[5]),
        )


def fisher_break_even(nominal_rate: float, real_rate: float) -> float:
    """π = (1+i)/(1+r) - 1, per BCRA TN 8/2024 Eq. 8."""
    return (1.0 + nominal_rate) / (1.0 + real_rate) - 1.0


def forward_rate(curve, t1: float, t2: float) -> Optional[float]:
    """Forward rate between t1 and t2, per NT 3/2019 Eqs. 8'/9'.

    F = ((1 + s_{0,t2})^t2 / (1 + s_{0,t1})^t1)^(1/(t2-t1)) - 1
    """
    if t2 <= t1 or t1 < 0:
        return None
    s1 = curve(t1)
    s2 = curve(t2)
    if s1 is None or s2 is None or s1 <= -1.0 or s2 <= -1.0:
        return None
    try:
        return ((1.0 + s2) ** t2 / (1.0 + s1) ** t1) ** (1.0 / (t2 - t1)) - 1.0
    except (ValueError, OverflowError):
        return None


def forward_bei_between_tenors(
    nom_curve, real_curve, t1: float, t2: float,
) -> Optional[float]:
    """Forward BEI between t1 and t2, per NT 3/2019 Eq. 10.

    π_fwd(t1→t2) = (1 + F^i_{t1,t2}) / (1 + F^r_{t1,t2}) − 1

    Reveals the inflation the market expects for the interval, distinct
    from the spot BEI (which averages from today to T).
    """
    fwd_nom = forward_rate(nom_curve, t1, t2)
    fwd_real = forward_rate(real_curve, t1, t2)
    if fwd_nom is None or fwd_real is None:
        return None
    return fisher_break_even(fwd_nom, fwd_real)


def real_fx_drift(devaluation_rate: float, inflation_rate: float) -> Optional[float]:
    """Real FX drift = (1+dev)/(1+inflation) − 1.

    Positive → the market prices in real depreciation (FX rises faster
    than IPC). Negative → real appreciation (peso strengthens in real
    terms despite nominal devaluation).
    """
    if devaluation_rate is None or inflation_rate is None or inflation_rate <= -1.0:
        return None
    return (1.0 + devaluation_rate) / (1.0 + inflation_rate) - 1.0


def gamma_known_cer_factor(
    cer_at_liq_minus_10h: Optional[float],
    cer_last_published: Optional[float],
) -> Optional[float]:
    """γ = CER_ULT_CONOCIDO / CER_LIQ-10h, per BCRA TN 8/2024 Eq. A4.

    Represents the already-known portion of CER variation between the
    liquidation-lagged reference and the most recent published value.
    """
    if not cer_at_liq_minus_10h or not cer_last_published or cer_at_liq_minus_10h <= 0:
        return None
    return cer_last_published / cer_at_liq_minus_10h


# ---------------------------------------------------------------------------
# Bootstrapping de tasas spot zero-coupon — NT N°3/2019 Eqs. 11-16
# ---------------------------------------------------------------------------

def bootstrap_zero_rates(
    bonds: Sequence[dict],
    today: date,
) -> List[Tuple[float, float]]:
    """Extract zero-coupon spot rates by bootstrapping.

    `bonds` is a list of dicts, each with:
      - "price":     full market price (clean+accrued not modelled separately)
      - "flows":     [(date, amount), ...] future cashflows from settlement
      - "vto":       date of final maturity (last flow)

    The algorithm sorts by maturity, then iteratively backs out the spot rate
    at each bond's final date by discounting earlier flows with the spot
    rates already extracted (linearly interpolated for in-between dates).

    Returns [(tenor_years, spot_rate_decimal)] sorted by tenor.
    """
    if not bonds:
        return []

    points: List[Tuple[float, float]] = []
    bonds_sorted = sorted(bonds, key=lambda b: b["vto"])

    for b in bonds_sorted:
        flows = sorted(b["flows"], key=lambda f: f[0])
        if not flows:
            continue
        final_d = flows[-1][0]
        final_t = (final_d - today).days / 365.25
        if final_t <= 0:
            continue

        # Single-flow bond → direct zero rate.
        if len(flows) == 1:
            cf_date, cf_amount = flows[0]
            if cf_amount <= 0 or b["price"] <= 0:
                continue
            spot = (cf_amount / b["price"]) ** (1.0 / final_t) - 1.0
            points.append((final_t, spot))
            points.sort()
            continue

        # Multi-flow bond → discount earlier flows with existing curve,
        # then solve for the spot rate that prices the residual.
        pv_known = 0.0
        residual_flow_amount = 0.0
        for cf_date, cf_amount in flows[:-1]:
            t_k = (cf_date - today).days / 365.25
            if t_k <= 0:
                continue
            r_k = _interp_rate(points, t_k)
            if r_k is None:
                # Insufficient prior data — skip this bond rather than fabricate.
                pv_known = None
                break
            pv_known += cf_amount / (1.0 + r_k) ** t_k
        if pv_known is None:
            continue

        residual_flow_amount = flows[-1][1]
        residual_pv = b["price"] - pv_known
        if residual_pv <= 0 or residual_flow_amount <= 0:
            continue
        spot = (residual_flow_amount / residual_pv) ** (1.0 / final_t) - 1.0
        points.append((final_t, spot))
        points.sort()

    return points


def _interp_rate(points: List[Tuple[float, float]], t: float) -> Optional[float]:
    """Linear interpolation on the bootstrap grid. Flat extrapolation."""
    if not points:
        return None
    if t <= points[0][0]:
        return points[0][1]
    if t >= points[-1][0]:
        return points[-1][1]
    for i in range(len(points) - 1):
        t0, r0 = points[i]
        t1, r1 = points[i + 1]
        if t0 <= t <= t1:
            return r0 + (r1 - r0) * (t - t0) / (t1 - t0)
    return None


# ---------------------------------------------------------------------------
# Método de pares de bonos — NT N°8/2024 Apéndice (Eqs. A4-A13)
# ---------------------------------------------------------------------------

def pair_delta(
    *,
    lecap_price: float,
    lecap_payment_at_maturity: float,
    cer_price: float,
    cer_principal_plus_coupon: float,
    cer_base: float,
    cer_liq_minus_10h: float,
    cer_last_known: float,
    days_to_maturity: int,
) -> Optional[float]:
    """Pair-of-bonds δ, per NT N°8/2024 Apéndice Eq. A13.

    δ = P_cer · (1 + i_lecap)^(d/365) / [(Principal + cupón) · (CER_LIQ-10h / CER_BASE) · γ]

    where:
      - i_lecap is the LECAP's TEA (derived implicitly from its zero-coupon price)
      - γ = CER_ULT / CER_LIQ-10h
      - δ = future CER variation from last known CER to final indexation date

    Returns δ − 1 (so 0.05 means +5% CER growth ahead). Returns None on
    invalid inputs.
    """
    if not all(x and x > 0 for x in (
        lecap_price, lecap_payment_at_maturity, cer_price,
        cer_principal_plus_coupon, cer_base, cer_liq_minus_10h, cer_last_known,
    )) or days_to_maturity <= 0:
        return None
    i_lecap = (lecap_payment_at_maturity / lecap_price) ** (365.0 / days_to_maturity) - 1.0
    gamma = cer_last_known / cer_liq_minus_10h
    denom = cer_principal_plus_coupon * (cer_liq_minus_10h / cer_base) * gamma
    if denom <= 0:
        return None
    numerator = cer_price * (1.0 + i_lecap) ** (days_to_maturity / 365.0)
    delta = numerator / denom
    return delta - 1.0


def pair_monthly_inflation(
    *,
    delta_minus_1: float,
    days_covered: int,
    days_per_month: int = 31,
) -> Optional[float]:
    """Annualize δ-1 to a monthly inflation expectation, NT 8/2024 Eq A12.

    Returns (1+δ)^(days_per_month/days_covered) − 1.
    """
    if delta_minus_1 is None or delta_minus_1 <= -1 or days_covered <= 0:
        return None
    return (1.0 + delta_minus_1) ** (days_per_month / days_covered) - 1.0
