"""Portfolio (Cartera) — agregación de tenencias. Dominio puro.

Toma tenencias (ticker + nominal + precio de costo) + métricas vivas por ticker
y agrega a nivel cartera: valor de mercado, P&L, TIR ponderada, MD de cartera,
exposición por grupo y por moneda. Más un calendario de flujos futuros escalado
por tenencia.

Puro: sin red, sin Excel, sin estado. Las unidades de `tir`/`md`/`spread_curva`
se preservan tal cual vienen del server (que pasa % units para tir/spread); las
agregaciones son promedios ponderados por valor de mercado en ARS. `pnl_pct` y
`weight` se devuelven como fracciones decimales (el frontend las escala a %).

Conversión de moneda: las patas que cotizan en dólares (sufijo D=MEP, C=CABLE de
soberanos / BOPREAL / ONs hard-dollar y dollar-linked / provinciales hard-dollar)
se convierten a ARS con el FX de SU pata: `fx_usd_ars` = MEP para las …D,
`fx_cable_ars` = CCL para las …C (si no se pasa CCL, la pata cable cae al MEP).
El costo se convierte al MISMO FX actual (v1: el P&L en ARS mide variación de
precio en USD expresada en pesos, no el drift del FX sobre la base de costo).

P&L: se computa SOLO sobre el subconjunto de posiciones con las dos puntas
conocidas (costo Y precio vivo). Mezclar el market value de todas contra el costo
de algunas daba un P&L sin sentido (una tenencia sin costo entraba entera como
ganancia). `cost_coverage` dice qué fracción del libro cubre ese P&L.
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from core.domain.currency import ccy_from_suffix

# Tipos cuya especie D/C liquida en dólares. Los soberanos/bopreales matchean por
# igualdad; el resto por SUBSTRING, igual que los predicados del motor
# (`Instrument.is_hard_dollar` / `is_dolar_linked`) — así "PROVINCIAL HARD DOLLAR"
# y las ONs quedan cubiertas sin enumerar cada variante.
_USD_TYPES = ("BONAR", "GLOBAL", "BOPREAL")
_USD_TOKENS = ("HARD DOLLAR", "DOLAR LINKED", "DOLLAR LINKED",
               "DOLAR_LINKED", "DOLLAR_LINKED")


def _pays_usd(instrument_type: Optional[str]) -> bool:
    """¿El instrumento cotiza en dólares en sus patas D/C?"""
    t = (instrument_type or "").upper().strip()
    return t in _USD_TYPES or any(tok in t for tok in _USD_TOKENS)


def position_currency(instrument_type: Optional[str], ticker: str) -> str:
    """USD para las patas MEP (…D) y CABLE (…C) de los tipos que cotizan en
    dólares; ARS para el resto.

    Convención de sufijo de agents.md ("D→MEP, C→CABLE, resto→ARS") acotada a los
    tipos que efectivamente liquidan en USD: soberanos / BOPREAL / ONs hard-dollar
    y dollar-linked / provinciales hard-dollar. El acote es necesario porque las
    ACCIONES también tienen especies …D/…C y no son posiciones de renta fija.

    Antes esto era `instrument_type in ("BONAR","GLOBAL","BOPREAL") and sufijo=="MEP"`:
    dejaba en ARS toda pata CABLE y toda ON/provincial …D — con lo que la cartera
    NO les aplicaba el FX (subvaluación ~1.400×) y /escenarios les daba beta 0 al
    dólar. Es la misma regla que el motor ya usa en `DolarLinkedStrategy._is_usd_leg`
    y `generate_report._sovereign_ars_usd_price`.
    """
    if _pays_usd(instrument_type) and ccy_from_suffix(ticker) in ("MEP", "CABLE"):
        return "USD"
    return "ARS"


def position_fx_leg(instrument_type: Optional[str], ticker: str) -> Optional[str]:
    """Pata de FX de una posición en dólares: "MEP" (…D) o "CABLE" (…C).
    None si la posición es en pesos (no hay conversión que hacer)."""
    if position_currency(instrument_type, ticker) != "USD":
        return None
    return ccy_from_suffix(ticker)


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # descarta NaN


def build_portfolio(
    holdings: List[dict],
    metrics_by_ticker: Dict[str, dict],
    *,
    fx_usd_ars: Optional[float] = None,
    fx_cable_ars: Optional[float] = None,
    as_of: Optional[date] = None,
) -> dict:
    """Valúa la cartera. Devuelve {"positions": [...], "summary": {...}}.

    `holdings`: [{"ticker", "nominal", "cost_price"?, "note"?}, ...]
    `metrics_by_ticker`: {TICKER: {"price","tir","md","grupo","spread_curva",
                          "currency","short_name"}} — lo arma el server desde el
                          snapshot vivo. Tickers ausentes → posición sin precio.
    `fx_usd_ars`: **MEP** (dólar bolsa) — es el tipo de cambio al que liquida una
                  pata …D. `fx_cable_ars`: CCL para las patas …C (default: el MEP).
    """
    as_of = as_of or date.today()
    fx = fx_usd_ars if (fx_usd_ars and fx_usd_ars > 0) else None
    fx_cable = fx_cable_ars if (fx_cable_ars and fx_cable_ars > 0) else None

    positions: List[dict] = []
    for h in holdings:
        tk = str(h.get("ticker", "")).upper().strip()
        if not tk:
            continue
        nominal = _num(h.get("nominal"))
        cost_price = _num(h.get("cost_price"))
        m = metrics_by_ticker.get(tk, {})
        ccy = m.get("currency") or "ARS"
        price = _num(m.get("price"))
        units = nominal / 100.0 if nominal is not None else None

        mv = units * price if (units is not None and price is not None) else None
        cv = units * cost_price if (units is not None and cost_price is not None) else None
        pnl = (mv - cv) if (mv is not None and cv is not None) else None
        pnl_pct = (price / cost_price - 1.0) if (price and cost_price) else None

        # FX por pata: la …D liquida al MEP y la …C al CCL (sin CCL → cae al MEP).
        rate = fx
        if ccy != "ARS" and ccy_from_suffix(tk) == "CABLE" and fx_cable:
            rate = fx_cable

        def _to_ars(x, _rate=rate, _ccy=ccy):
            if x is None:
                return None
            return x if _ccy == "ARS" else (x * _rate if _rate else None)

        positions.append({
            "ticker": tk,
            "short_name": m.get("short_name"),
            "grupo": m.get("grupo"),
            "currency": ccy,
            "nominal": nominal,
            "price": price,
            "cost_price": cost_price,
            "tir": m.get("tir"),
            "md": m.get("md"),
            "spread_curva": m.get("spread_curva"),
            "market_value": mv,
            "cost_value": cv,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "market_value_ars": _to_ars(mv),
            "cost_value_ars": _to_ars(cv),
            "note": h.get("note") or "",
        })

    total_mv = sum(p["market_value_ars"] for p in positions if p["market_value_ars"]) or 0.0
    total_cost = sum(p["cost_value_ars"] for p in positions if p["cost_value_ars"]) or 0.0

    for p in positions:
        mva = p["market_value_ars"]
        p["weight"] = (mva / total_mv) if (mva and total_mv) else None

    def _wavg(field: str) -> Optional[float]:
        num = den = 0.0
        for p in positions:
            w = p["market_value_ars"]
            v = p.get(field)
            if w and v is not None:
                num += w * v
                den += w
        return num / den if den else None

    by_grupo: Dict[str, float] = {}
    by_currency: Dict[str, float] = {}
    for p in positions:
        if p["market_value_ars"]:
            by_grupo[p["grupo"] or "—"] = by_grupo.get(p["grupo"] or "—", 0.0) + p["market_value_ars"]
        if p["market_value"]:
            by_currency[p["currency"]] = by_currency.get(p["currency"], 0.0) + p["market_value"]

    # P&L sobre el subconjunto con AMBAS puntas conocidas (costo Y precio vivo):
    # mezclar `total_mv` (todas) con `total_cost` (solo las que tienen costo) hacía
    # que una tenencia sin costo entrara entera como ganancia, y una con costo pero
    # sin precio restara sin compensación.
    priced = [p for p in positions if p["cost_value_ars"] and p["market_value_ars"]]
    cost_base = sum(p["cost_value_ars"] for p in priced) or 0.0
    mv_base = sum(p["market_value_ars"] for p in priced) or 0.0
    pnl_ars = (mv_base - cost_base) if cost_base else None
    summary = {
        "as_of": as_of.isoformat(),
        "total_market_value_ars": total_mv,
        "total_cost_ars": total_cost or None,
        "pnl_ars": pnl_ars,
        "pnl_pct": (mv_base / cost_base - 1.0) if cost_base else None,
        # Qué fracción del valor del libro cubre ese P&L (1.0 = toda la cartera).
        "cost_coverage": (mv_base / total_mv) if (cost_base and total_mv) else None,
        "weighted_tir": _wavg("tir"),
        "portfolio_md": _wavg("md"),
        "weighted_spread_curva": _wavg("spread_curva"),
        "by_grupo": by_grupo,
        "by_currency": by_currency,
        "n_positions": len([p for p in positions if p["nominal"]]),
        "fx_usd_ars": fx,
        "fx_cable_ars": fx_cable,
    }
    # Orden: por peso desc (las posiciones sin precio al final).
    positions.sort(key=lambda p: (p["weight"] is None, -(p["weight"] or 0.0)))
    return {"positions": positions, "summary": summary}


def portfolio_cashflows(
    holdings: List[dict],
    instruments_by_ticker: Dict[str, object],
    *,
    today: Optional[date] = None,
    months_ahead: int = 24,
) -> dict:
    """Calendario de flujos futuros escalado por tenencia, agrupado por mes.

    Devuelve {"months": [{"month": "YYYY-MM", "ars": x, "usd": y}],
              "events": [{"date","ticker","amount","currency","kind"}]}.

    NOTA v1: usa los cashflows en términos "base" del Instrument. Para
    CER/TAMAR/DL los flujos reales se indexan al pago (CER ratio / FX), así que
    estos montos son aproximados (los hard-dollar USD sí son exactos).
    """
    today = today or date.today()
    buckets: Dict[str, Dict[str, float]] = {}
    events: List[dict] = []

    for h in holdings:
        tk = str(h.get("ticker", "")).upper().strip()
        nominal = _num(h.get("nominal"))
        if not tk or not nominal:
            continue
        inst = instruments_by_ticker.get(tk)
        if inst is None:
            continue
        units = nominal / 100.0
        ccy = position_currency(getattr(inst, "instrument_type", None), tk)
        for cf in inst.get_future_cashflows(today):
            amt = units * cf.total
            if not amt:
                continue
            ym = cf.date.strftime("%Y-%m")
            b = buckets.setdefault(ym, {"ars": 0.0, "usd": 0.0})
            b["usd" if ccy == "USD" else "ars"] += amt
            events.append({
                "date": cf.date.isoformat(),
                "ticker": tk,
                "amount": amt,
                "currency": ccy,
                "kind": "amort+renta" if (cf.amortization and cf.interest)
                         else ("amortización" if cf.amortization else "renta"),
            })

    months = [{"month": ym, **vals} for ym, vals in sorted(buckets.items())][:months_ahead]
    events.sort(key=lambda e: e["date"])
    return {"months": months, "events": events}
