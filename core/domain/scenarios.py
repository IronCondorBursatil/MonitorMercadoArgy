"""Escenarios / stress de tasa y FX — dominio puro.

Reprice analítico por duración + convexidad ante un shock de tasa (en bps) y un
shock de FX (en %). Sin red, sin estado: funciona sobre las métricas que ya
publica cada panel (precio, MD, convexidad, moneda, grupo).

    ΔP/P (precio cotizado) ≈ (1 − MD·Δy + ½·C·Δy²)·(1 + β_precio·ΔFX) − 1
    ΔP/P (valor en ARS)    = (1 + ΔP_cotizado)·(1 + β_valor·ΔFX) − 1

Betas de FX por tipo (quién se mueve ante una devaluación):
- Hard-dollar USD (sufijo D): el precio en USD NO se mueve con el FX, pero su
  valor en ARS escala 1:1 → β_precio=0, β_valor=1.
- Dólar-linked: cotiza en ARS pero el payoff sigue al FX → β_precio=1, β_valor=0.
- CER / tasa fija / TAMAR (pesos): insensibles al FX → β_precio=0, β_valor=0.

La convexidad es opcional: si falta (paneles que no la publican) el reprice es
lineal en MD (subestima levemente el rebote ante shocks grandes).

Para escenarios de INFLACIÓN por sendero mensual ver `bond_detail.cer_return_
scenarios` (/api/cer_scenarios) — más riguroso para bonos CER que un shock de
tasa real plano.
"""
from __future__ import annotations

from typing import List, Optional, Tuple


def fx_betas(currency: Optional[str], grupo: Optional[str]) -> Tuple[float, float]:
    """Devuelve (β_precio, β_valor) según el tipo de posición."""
    g = (grupo or "").upper()
    if currency == "USD":
        return 0.0, 1.0
    if g == "DL" or "LINKED" in g:
        return 1.0, 0.0
    return 0.0, 0.0


def price_shock_pct(md: Optional[float], convexity: Optional[float],
                    d_tir_bps: float) -> Optional[float]:
    """Efecto tasa puro: −MD·Δy + ½·C·Δy². None si no hay MD."""
    if md is None:
        return None
    dy = (d_tir_bps or 0.0) / 10000.0
    eff = -md * dy
    if convexity:
        eff += 0.5 * convexity * dy * dy
    return eff


def shock_position(*, price: Optional[float], md: Optional[float],
                   convexity: Optional[float] = None,
                   currency: Optional[str] = None, grupo: Optional[str] = None,
                   d_tir_bps: float = 0.0, d_fx_pct: float = 0.0) -> dict:
    """Reprice de una posición ante (Δtir, ΔFX). Devuelve dp_pct (moneda del
    bono), dp_ars_pct (valor en ARS) y new_price."""
    rate = price_shock_pct(md, convexity, d_tir_bps)
    b_price, b_value = fx_betas(currency, grupo)
    fx = (d_fx_pct or 0.0) / 100.0

    dp_quoted = None
    if rate is not None:
        dp_quoted = (1.0 + rate) * (1.0 + b_price * fx) - 1.0
    elif b_price and fx:
        dp_quoted = b_price * fx

    dp_ars = None
    if dp_quoted is not None:
        dp_ars = (1.0 + dp_quoted) * (1.0 + b_value * fx) - 1.0

    new_price = price * (1.0 + dp_quoted) if (price is not None and dp_quoted is not None) else None
    return {
        "dp_pct": dp_quoted,
        "dp_ars_pct": dp_ars,
        "new_price": new_price,
        "fx_beta_price": b_price,
        "fx_beta_value": b_value,
    }


def portfolio_shock(positions: List[dict], *, d_tir_bps: float = 0.0,
                    d_fx_pct: float = 0.0) -> dict:
    """Agrega el P&L del libro ante un escenario.

    `positions`: [{ticker, market_value_ars, md, convexity?, currency, grupo}].
    Pondera el ΔP en ARS por el valor de mercado de cada posición.
    Devuelve {pnl_ars, pnl_pct, base_value_ars, positions:[...]}.
    """
    base = 0.0
    pnl = 0.0
    out: List[dict] = []
    for p in positions:
        mv = p.get("market_value_ars")
        sh = shock_position(
            price=p.get("price"), md=p.get("md"), convexity=p.get("convexity"),
            currency=p.get("currency"), grupo=p.get("grupo"),
            d_tir_bps=d_tir_bps, d_fx_pct=d_fx_pct,
        )
        contrib = None
        if mv and sh["dp_ars_pct"] is not None:
            contrib = mv * sh["dp_ars_pct"]
            base += mv
            pnl += contrib
        out.append({**p, **sh, "pnl_ars": contrib})
    return {
        "pnl_ars": pnl if base else None,
        "pnl_pct": (pnl / base) if base else None,
        "base_value_ars": base or None,
        "positions": out,
    }
