"""Builder de la chain enriquecida: combina opciones live + spots + cómputos.

`build_options(options_rows, stocks_rows, r, q)` toma:
  - options_rows: {symbol: Data912Row} con sólo opciones (filtrado por source)
  - stocks_rows: {ticker: Data912Row} de los subyacentes
  - r, q: tasa libre de riesgo + dividend yield (decimales, anuales)

y devuelve `list[OptionItem]` — una por contrato válido, con strike, expiry,
mid, IV implícita, griegos, tasas implícitas. Las opciones cuyo root no está
mapeado (roots.py) o cuyo subyacente no tiene quote en `stocks_rows` se
descartan silenciosamente.

El costo total es O(N) en el número de opciones; para ~1050 contratos con
N=80 pasos CRR, ~5-8 segundos (medido: 6,08s / 458 contratos en el ARM del
servidor). Por eso corre en `to_thread` desde su propio `_options_loop` — NO
desde el refresh de precios, que es de 5s — y se publica con
`AppState.set_options`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from core.domain.options.expiry import days_to_expiry, resolve_expiry_date, time_to_expiry
from core.domain.options.greeks import Greeks, compute_greeks
from core.domain.options.models import OptionContract
from core.domain.options.pricing import iv_implied
from core.domain.options.rates import ImpliedRates, compute_rates
from core.domain.options.roots import underlying_for
from core.domain.options.symbols import parse_ticker, resolve_strike

logger = logging.getLogger(__name__)


def _expiry_from_iso(s: Optional[str]) -> Optional[date]:
    """Parsea un vencimiento ISO 'YYYY-MM-DD' (maturityDate de BYMA). None si falta
    o no parsea → el caller cae al cálculo por código de mes."""
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class OptionItem:
    """Unidad enriquecida que la UI consume directamente."""
    contract: OptionContract
    spot: float
    bid: float
    ask: float
    last: float
    mid: Optional[float]
    volume: float
    open_interest: float
    pct_change: Optional[float]
    iv: Optional[float]
    greeks: Greeks
    rates: ImpliedRates
    t_days: int

    # Atajos para serialización plana (templates / JSON).
    @property
    def ticker(self) -> str: return self.contract.ticker

    @property
    def underlying(self) -> str: return self.contract.underlying

    @property
    def kind(self) -> str: return self.contract.kind

    @property
    def strike(self) -> float: return self.contract.strike

    @property
    def expiry(self) -> date: return self.contract.expiry

    @property
    def month_code(self) -> str: return self.contract.month_code

    @property
    def moneyness_pct(self) -> Optional[float]:
        """Distancia porcentual al ATM (signed: + = ITM)."""
        if not self.spot:
            return None
        diff = (self.spot - self.strike) / self.spot
        return diff if self.kind == "C" else -diff

    def to_dict(self) -> dict:
        """Plano para JSON / templates."""
        return {
            "ticker": self.ticker, "root": self.contract.root,
            "underlying": self.underlying, "kind": self.kind,
            "kind_label": "CALL" if self.kind == "C" else "PUT",
            "strike": self.strike, "month": self.month_code,
            "expiry": self.expiry.isoformat(), "t_days": self.t_days,
            "spot": self.spot, "bid": self.bid, "ask": self.ask, "last": self.last,
            "mid": self.mid, "volume": self.volume, "oi": self.open_interest,
            "pct_change": self.pct_change,
            "iv": self.iv, "iv_pct": self.iv * 100 if self.iv is not None else None,
            "delta": self.greeks.delta, "gamma": self.greeks.gamma,
            "theta": self.greeks.theta, "vega": self.greeks.vega, "rho": self.greeks.rho,
            "tna_bruta": self.rates.tna_bruta, "tea_bruta": self.rates.tea_bruta,
            "tna_strike": self.rates.tna_strike,
            "moneyness_pct": self.moneyness_pct,
        }


def build_options(options_rows: dict, stocks_rows: dict,
                  r: float = 0.40, q: float = 0.0,
                  today: Optional[date] = None,
                  N: int = 80) -> list[OptionItem]:
    """Pipeline completo: filas crudas → OptionItem enriquecido.

    `options_rows` y `stocks_rows` son {symbol: Data912Row} (el hub ya los validó).
    `r` y `q` en decimales anuales. `N` = pasos del árbol CRR.
    """
    today = today or date.today()
    out: list[OptionItem] = []
    skipped_root: set[str] = set()

    for sym, row in options_rows.items():
        meta = parse_ticker(sym)
        if not meta:
            continue
        # Subyacente: preferir el `underlyingSymbol` AUTORITATIVO de BYMA (panel
        # /options) sobre el mapeo root→ticker de roots.py. Así los contratos cuyo
        # root no está en roots.py (VIST, TGNO4, …) sobreviven si tenemos su spot.
        # Data912 no trae el campo → cae al mapeo histórico (back-compat).
        underlying = getattr(row, "opt_underlying", None) or underlying_for(meta.root)
        if not underlying:
            skipped_root.add(meta.root)
            continue
        stock = stocks_rows.get(underlying)
        if not stock:
            continue
        spot = float(stock.c or 0.0)
        if spot <= 0:
            continue
        strike = resolve_strike(meta.strike_str, spot)
        if strike is None or strike <= 0:
            continue
        # Tipo (C/V): preferir optionType de BYMA; si no, el del ticker.
        kind = getattr(row, "opt_kind", None) or meta.kind
        # Vencimiento: preferir maturityDate de BYMA (exacto); si no, derivarlo del
        # código de mes (3er viernes). Un ISO inválido cae al fallback.
        expiry = _expiry_from_iso(getattr(row, "opt_expiry", None)) \
            or resolve_expiry_date(meta.month, today=today)
        # CONTRATO VENCIDO → fuera. `days_to_expiry` hace `max(1, ...)`, o sea que
        # disfraza el vencimiento y anula la única guarda del pipeline (rates.py
        # exige t_days > 0). Sin esto, una cohorte vencida sobrevive en el snapshot
        # del hub (que no purga) y sale con TNA basura (401%–8.922% medidos) que el
        # sort default del scanner —TNA desc— pone ARRIBA de las series vivas; peor,
        # los códigos de mes se repiten cada año, así que la cohorte muerta se mezcla
        # con su homónima viva. Muerde tras UN solo vencimiento sin reiniciar.
        if expiry < today:
            continue
        t_days = days_to_expiry(expiry, today)
        T = time_to_expiry(expiry, today)

        bid = float(row.px_bid or 0.0)
        ask = float(row.px_ask or 0.0)
        last = float(row.c or 0.0)
        mid: Optional[float]
        if bid > 0 and ask > 0:
            mid = 0.5 * (bid + ask)
        elif last > 0:
            mid = last
        else:
            mid = None

        iv = iv_implied(mid, spot, strike, r, q, T, kind, N=N) if mid else None
        greeks = (compute_greeks(spot, strike, r, q, iv, T, kind, N=N)
                  if iv else Greeks(None, None, None, None, None))
        rates = compute_rates(bid, spot, strike, kind, t_days, last=last)

        contract = OptionContract(
            ticker=meta.ticker, root=meta.root, underlying=underlying,
            kind=kind, strike=strike, month=meta.month,
            month_code=meta.month_code, expiry=expiry,
        )
        # Open interest: el REAL de BYMA (`oi`) si vino; si no, el q_op de Data912
        # (nº de trades — proxy histórico, no OI verdadero).
        oi_val = getattr(row, "oi", None)
        open_interest = float(oi_val) if oi_val is not None else float(row.q_op or 0.0)
        out.append(OptionItem(
            contract=contract, spot=spot, bid=bid, ask=ask, last=last, mid=mid,
            volume=float(row.v or 0.0), open_interest=open_interest,
            pct_change=row.pct_change,
            iv=iv, greeks=greeks, rates=rates, t_days=t_days,
        ))

    if skipped_root:
        logger.debug("options: %d roots desconocidos (skipped): %s",
                     len(skipped_root), sorted(skipped_root))
    return out


def underlyings_summary(items: Iterable[OptionItem]) -> list[dict]:
    """Resumen por subyacente para el sidebar (watchlist): {ticker, spot, vol_total, n_contratos}."""
    by_u: dict[str, dict] = {}
    for it in items:
        u = it.underlying
        if u not in by_u:
            by_u[u] = {"underlying": u, "spot": it.spot, "vol_total": 0.0, "n": 0}
        by_u[u]["vol_total"] += it.volume
        by_u[u]["n"] += 1
    return sorted(by_u.values(), key=lambda x: x["vol_total"], reverse=True)


def months_for(items: Iterable[OptionItem], underlying: str) -> list[str]:
    """Códigos de mes disponibles para un underlying, ordenados por fecha real."""
    seen: dict[str, date] = {}
    for it in items:
        if it.underlying == underlying:
            seen.setdefault(it.month_code, it.expiry)
    return [m for m, _ in sorted(seen.items(), key=lambda kv: kv[1])]
