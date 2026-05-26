"""Cashflow synthesis — pure functions sin dependencias de I/O ni instancias.

Sintetiza el schedule de cashflows de un bono a partir de sus parámetros
contractuales (cupón, frecuencia, vto, amortización, etc.). Se usa cuando
la hoja Cashflows del master Excel no tiene filas explícitas para el ticker.

Compartido entre:
  - ExcelInstrumentsRepository (fallback al cargar instrumentos)
  - apps.web.instruments_abm (preview en el form ABM)

Antes este código vivía en `repositories.py::_generate_bond_cashflows` y la
ABM lo accedía vía `ExcelInstrumentsRepository.__new__()` para esquivar
`__init__` — un hack frágil. Extraerlo acá lo hace testeable directamente.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional

from dateutil.relativedelta import relativedelta

from core.domain.models import Cashflow

logger = logging.getLogger(__name__)


# Tokens en el campo `tipo` / `clase` que indican zero-coupon (payoff único
# de 100 al vencimiento). LECAP/BONCAP se manejan aparte porque su payoff
# capitaliza desde tem_licit.
_ZC_TYPE_TOKENS = ("LECER", "ZC")  # nota: LECAP excluido a propósito, ver _synth_lecap


# --------------------------------------------------------------------------- #
# Helpers — parsing tolerante de campos del row (puede venir de Excel o ABM)
# --------------------------------------------------------------------------- #

def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        if val is None or val == "":
            return default
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _get_date(row: Mapping[str, Any], candidates: tuple) -> Optional[date]:
    """Devuelve la primera fecha parseable entre los candidates.

    IMPORTANTE: normaliza datetime → date.date(). Sin esto, openpyxl carga
    las fechas del Excel como datetime.datetime y el engine comparaba
    `cf.date >= settle_date` con tipos mixtos (TypeError). pandas.Timestamp
    también requiere la normalización (`.date()`).
    """
    for c in candidates:
        if c not in row:
            continue
        v = row[c]
        if v is None or v == "":
            continue
        # Orden importante: datetime es subclase de date → chequear primero.
        if isinstance(v, datetime):
            return v.date()
        # pandas.Timestamp también tiene .date()
        if hasattr(v, "date") and callable(v.date) and not isinstance(v, date):
            try:
                return v.date()
            except (TypeError, AttributeError):
                pass
        if isinstance(v, date):
            return v
        # strings: ISO YYYY-MM-DD o DD/MM/YYYY
        s = str(v).strip()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s[:len(fmt)+2], fmt).date()
            except (ValueError, TypeError):
                continue
    return None


def _parse_coupon_rate(raw: Any, asof: date) -> Optional[float]:
    """Cupón como decimal (0.05 = 5%). Soporta step-up '2024-12-31:0.63;2027-12-31:1.18'
    (devuelve la tasa activa a `asof`)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) / 100.0
    s = str(raw).strip()
    if not s:
        return None
    if ";" in s and ":" in s:
        try:
            pairs = []
            for entry in s.split(";"):
                d_str, r_str = entry.split(":")
                d = _get_date({"d": d_str.strip()}, ("d",))
                if d is not None:
                    pairs.append((d, float(r_str.strip()) / 100.0))
            pairs.sort()
            applicable = [r for d, r in pairs if d <= asof]
            return applicable[-1] if applicable else (pairs[0][1] if pairs else None)
        except (ValueError, TypeError):
            return None
    try:
        return float(s) / 100.0
    except ValueError:
        return None


def days_30_360(start: date, end: date) -> int:
    """Day-count 30/360 estándar (ISDA). Asume meses de 30 días y año de 360."""
    d1 = min(start.day, 30)
    d2 = end.day
    if d2 == 31 and d1 >= 30:
        d2 = 30
    return (end.year - start.year) * 360 + (end.month - start.month) * 30 + (d2 - d1)


# --------------------------------------------------------------------------- #
# Per-bond-type synth strategies
# --------------------------------------------------------------------------- #

def _synth_lecap_boncap(row: Mapping[str, Any], vto: date) -> List[Cashflow]:
    """LECAP/BONCAP capitalizable: payoff único al vto = 100 × (1+TEM)^N meses.

    Convención AR: el plazo en meses usa day-count 30/360 (NO actual/360).
    Para S29Y6 (emis 2025-05-30, vto 2026-05-29): días corridos = 364 →
    months = 12.13 da payoff sobre-estimado; con 30/360 → 359 días → 11.97
    meses → payoff 132.05 (matchea Balanz).
    """
    tem = _safe_float(row.get("tem_licit"), default=0.0)
    emision = _get_date(row, ("fecha_emision", "fecha emision"))
    if not (tem > 0 and emision and vto > emision):
        return [Cashflow(date=vto, amortization=100.0, interest=0.0)]
    base = str(row.get("base calculo", row.get("base_calculo", "")) or "").strip().lower()
    if "act" in base:
        months = (vto - emision).days / 30.0
    else:
        months = days_30_360(emision, vto) / 30.0
    payoff = 100.0 * (1.0 + tem) ** months
    return [Cashflow(date=vto, amortization=payoff, interest=0.0)]


def _synth_zero_coupon(_row: Mapping[str, Any], vto: date) -> List[Cashflow]:
    """LECER / BONCER ZC: payoff único de 100 al vto."""
    return [Cashflow(date=vto, amortization=100.0, interest=0.0)]


def _synth_coupon_bond(row: Mapping[str, Any], vto: date) -> List[Cashflow]:
    """Bullet o amortizing con cupón. Soporta step-up via `cupon anual %`.

    Convención AR: para amortizing, los cupones se anclan a `amort_inicio`
    (no a `emision`) — AL29D paga cupones y amorts el mismo día (Jul-9 / Jan-9).
    """
    coupon_rate = _parse_coupon_rate(
        row.get("cupon anual %", row.get("cupon")), asof=date.today()
    )
    if coupon_rate is None:
        return []

    freq = _safe_int(row.get("frecuencia pagos", row.get("frecuencia")), default=2)
    if freq <= 0:
        freq = 2
    months_between = max(12 // freq, 1)

    emision = _get_date(row, ("fecha_emision", "fecha emision")) \
        or (vto - relativedelta(years=2))

    capital_factor = _safe_float(row.get("capital factor"), default=1.0)
    if capital_factor <= 0:
        capital_factor = 1.0
    nominal_initial = 100.0 * capital_factor

    amort_inicio = _get_date(row, ("amort_inicio", "amort inicio"))
    amort_count = _safe_int(row.get("amort cantidad"), default=0)
    is_amortizing = (
        "amortizing" in str(row.get("tipo amortizacion", "")).lower()
        and amort_inicio is not None
        and amort_count > 0
    )

    coupon_dates: List[date] = []
    anchor = amort_inicio if is_amortizing else emision
    cd = anchor
    while cd > emision:
        cd = cd - relativedelta(months=months_between)
    cd = cd + relativedelta(months=months_between)
    while cd <= vto:
        if cd > emision:
            coupon_dates.append(cd)
        cd = cd + relativedelta(months=months_between)
    if not coupon_dates or coupon_dates[-1] != vto:
        coupon_dates.append(vto)

    amort_map: Dict[date, float] = {}
    if is_amortizing:
        per_installment = nominal_initial / amort_count
        ad = amort_inicio
        remaining = nominal_initial
        placed = 0
        while placed < amort_count and ad <= vto and remaining > 1e-9:
            amount = min(per_installment, remaining)
            amort_map[ad] = amort_map.get(ad, 0.0) + amount
            remaining -= amount
            placed += 1
            ad = ad + relativedelta(months=months_between)
        if remaining > 1e-9:
            amort_map[vto] = amort_map.get(vto, 0.0) + remaining
    else:
        amort_map[vto] = nominal_initial

    cfs: List[Cashflow] = []
    outstanding = nominal_initial
    all_dates = sorted(set(coupon_dates) | set(amort_map.keys()))
    for d in all_dates:
        interest = outstanding * coupon_rate / freq if d in coupon_dates else 0.0
        amort = amort_map.get(d, 0.0)
        outstanding = max(outstanding - amort, 0.0)
        cfs.append(Cashflow(date=d, amortization=amort, interest=interest))
    return cfs


# --------------------------------------------------------------------------- #
# Public entry point — dispatch por tipo
# --------------------------------------------------------------------------- #

def synth_cashflows(row: Mapping[str, Any]) -> List[Cashflow]:
    """Sintetiza cashflows desde los parámetros contractuales del bono.

    `row` es un mapping con keys normalizadas a lowercase (las columnas Excel
    o los fields del form ABM). Acepta tanto pandas.Series como dict.

    Edge cases:
      - Sin fecha de vencimiento → retorna [].
      - vto <= emision → log warning + retorna [] (bond inválido).
      - Tipo desconocido sin cupón → retorna [].
    """
    vto = _get_date(row, (
        "fecha_vencimiento", "fecha vencimiento", "fecha_pago", "maturity",
    ))
    if not vto:
        return []

    emision = _get_date(row, ("fecha_emision", "fecha emision"))
    if emision and vto <= emision:
        ticker = row.get("ticker") or row.get("short_name") or "?"
        logger.warning(
            f"synth_cashflows({ticker}): vto {vto} <= emisión {emision} — bond inválido, skip"
        )
        return []

    itype = str(row.get("tipo", row.get("clase", ""))).upper()

    # Dispatch table — orden importa (LECAP antes que LECER por substring).
    if "LECAP" in itype or "BONCAP" in itype:
        return _synth_lecap_boncap(row, vto)
    if any(t in itype for t in _ZC_TYPE_TOKENS):
        return _synth_zero_coupon(row, vto)
    return _synth_coupon_bond(row, vto)
