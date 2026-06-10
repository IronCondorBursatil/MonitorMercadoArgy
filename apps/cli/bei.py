"""Break-Even Inflation monitor — extendido.

Implementa la metodología completa de BCRA NT N°3/2019 (Corso & Matarrelli) +
NT N°8/2024 (Matarrelli & Pastore):

  1. Bootstrapping de tasas spot zero-coupon a partir de bonos cuponados
     (NT3 Eq. 11-16). Para LECAP/BONCAP zero-coupon es directo; para BONCER
     cuponados se descuentan los cupones intermedios.
  2. Ajuste paramétrico de curva: NSS (6 params, NT3 Eq. 17) con fallback
     NS (NT8 Eq. 11) y lineal si falta data.
  3. BEI spot vía Fisher (NT8 Eq. 8) y BEI forward entre tenors (NT3 Eq. 10).
  4. Sendero mensual de inflación implícita (Fig. 4 NT8, próximos 12 meses)
     comparado contra la mediana del REM-BCRA mes a mes.
  5. Método de pares (NT8 §2.2 + Apéndice Eq. A13) como cross-check para
     pares LECAP/CER con vencimiento cercano.
  6. Tercer rail: curva TAMAR forward + BEI TAMAR.
  7. Curva de devaluación implícita ajustada a futuros DLR.
  8. TC real drift (Fisher devaluación vs BEI).
  9. Ajuste γ (CER_ULT / CER_LIQ-10h) opcional sobre BEI spot.
 10. Persistencia diaria en data/history/bei_diario.csv.

API:
  compute_bei_tables() → dict con todos los números (sin formatear). Lo consume
                         el web server (lo expone como /api/snapshot extendido).
"""

import csv
import logging
import os
from datetime import date, timedelta
from typing import Any, Callable, List, Optional, Tuple

import numpy as np

from apps.cli._common import build_use_case
from config.settings import settings
from core.domain.inflation_path import monthly_inflation_path
from core.domain.instrument_groups import CER, DUAL_TAMAR, TAMAR, TASA_FIJA
from core.domain.yield_curve import (
    NelsonSiegelCurve,
    NelsonSiegelSvenssonCurve,
    bootstrap_zero_rates,
    fisher_break_even,
    forward_bei_between_tenors,
    gamma_known_cer_factor,
    pair_delta,
    pair_monthly_inflation,
    real_fx_drift,
)
from core.holiday_engine import is_habil, settlement_byma
from core.infrastructure.fx_provider import DolarAPIProvider
from core.infrastructure.futures_provider import (
    DEFAULT_SYMBOLS as ROFEX_SYMBOLS,
    RofexProvider,
    implied_tna,
    parse_contract_maturity,
    resolve_spot_for_tna,
)
from core.infrastructure.indices_provider import BCRAIndicesProvider
from core.infrastructure.rem_provider import REMProvider

logger = logging.getLogger(__name__)

# Deduplicación del warning "DLR SPOT no disponible". Sin esto, el BEI
# thread (refresh 5 min, 24×7) genera ~288 líneas/día idénticas cuando el
# spot no está disponible fuera de rueda o BCRA A3500 no responde.
# Patrón: 1ª falla → log normal. Siguientes → silencio. Resumen cada 60 min.
_DLR_SPOT_WARN_LOCK = __import__("threading").Lock()
_dlr_spot_fail_count: int = 0
_dlr_spot_first_fail_ts: float = 0.0
_dlr_spot_last_summary_ts: float = 0.0
_DLR_SPOT_SUMMARY_INTERVAL_S: float = 3600.0


def _log_dlr_spot_missing() -> None:
    """Log inteligente del warning 'DLR SPOT no disponible'.
    - 1ª vez en el día: log WARNING normal.
    - Posteriores: silencio hasta el siguiente resumen (cada 60 min).
    """
    import time as _time
    global _dlr_spot_fail_count, _dlr_spot_first_fail_ts, _dlr_spot_last_summary_ts
    now = _time.monotonic()
    with _DLR_SPOT_WARN_LOCK:
        _dlr_spot_fail_count += 1
        if _dlr_spot_fail_count == 1:
            _dlr_spot_first_fail_ts = now
            logger.warning("DLR SPOT no disponible — curva de devaluación implícita omitida.")
            _dlr_spot_last_summary_ts = now
        elif (now - _dlr_spot_last_summary_ts) >= _DLR_SPOT_SUMMARY_INTERVAL_S:
            elapsed = now - _dlr_spot_first_fail_ts
            logger.warning(
                "DLR SPOT sigue sin datos (%.0f min, %d intentos fallidos) "
                "— curva de devaluación omitida.",
                elapsed / 60, _dlr_spot_fail_count,
            )
            _dlr_spot_last_summary_ts = now


def _reset_dlr_spot_warn() -> None:
    """Llamar cuando el spot DLR vuelve a estar disponible."""
    global _dlr_spot_fail_count, _dlr_spot_first_fail_ts, _dlr_spot_last_summary_ts
    with _DLR_SPOT_WARN_LOCK:
        if _dlr_spot_fail_count > 0:
            logger.info(
                "DLR SPOT disponible nuevamente (tras %d intentos fallidos).",
                _dlr_spot_fail_count,
            )
        _dlr_spot_fail_count = 0
        _dlr_spot_first_fail_ts = 0.0
        _dlr_spot_last_summary_ts = 0.0


_TENORS = [
    ("3M", 0.25), ("6M", 0.50), ("9M", 0.75), ("1Y", 1.00),
    ("18M", 1.50), ("2Y", 2.00), ("3Y", 3.00),
]

_HISTORY_CSV = os.path.join(str(settings.history_dir), "bei_diario.csv")
_HISTORY_COLS = [
    "fecha", "tenor_label", "tenor_years",
    "tea_nominal", "tea_real", "tamar_fwd",
    "bei_spot", "bei_fwd_lag", "bei_tamar",
    "dev_implicita", "tc_real_drift",
]


def _curve_range(points: List[Tuple[float, float]]) -> Tuple[Optional[float], Optional[float]]:
    """Min/max tenor observed; (None, None) if no points."""
    if not points:
        return (None, None)
    ts = [p[0] for p in points]
    return (min(ts), max(ts))


def _eval_in_range(curve: Optional[Callable[[float], float]], t: float,
                   t_range: Tuple[Optional[float], Optional[float]]) -> Optional[float]:
    """Evaluate `curve(t)` only when t is within ±10% of the observed range.
    Returns None outside that band (avoids reporting wild NS/NSS extrapolation)."""
    if curve is None:
        return None
    lo, hi = t_range
    if lo is None or hi is None or t < lo * 0.9 or t > hi * 1.1:
        return None
    return curve(t)


def _fit_curve(points: List[Tuple[float, float]], label: str) -> Tuple[Optional[Any], Optional[str]]:
    """NSS (≥4) → NS (≥3) → linear (≥2)."""
    if len(points) >= 4:
        try:
            return NelsonSiegelSvenssonCurve.fit(*zip(*points)), "NSS"
        except (ValueError, RuntimeError) as e:
            logger.warning(f"{label}: NSS fit failed ({e}); falling back to NS.")
    if len(points) >= 3:
        try:
            return NelsonSiegelCurve.fit(*zip(*points)), "NS"
        except (ValueError, RuntimeError) as e:
            logger.warning(f"{label}: NS fit failed ({e}); falling back to linear.")
    if len(points) >= 2:
        pts = sorted(points)
        ts, ys = [p[0] for p in pts], [p[1] for p in pts]

        class _LinearCurve:
            def __call__(self, t: float) -> float:
                return float(np.interp(t, ts, ys, left=ys[0], right=ys[-1]))

        return _LinearCurve(), "Lin"
    return None, None


def _gamma_factor(indices_provider, settle_date: date):
    target = settle_date
    count = 0
    while count < 10:
        target -= timedelta(days=1)
        if is_habil(target.strftime("%Y-%m-%d")):
            count += 1
    cer_liq_10h = indices_provider.get_cer(target)
    cer_last = indices_provider.get_cer(date.today())
    return gamma_known_cer_factor(cer_liq_10h, cer_last), cer_liq_10h, cer_last


def _bonds_for_bootstrap_nominal(metrics_list, today: date) -> list:
    bonds = []
    for m in metrics_list:
        inst = m.snapshot.instrument
        if not inst or not m.snapshot.price or not inst.cashflows:
            continue
        flows = [(cf.date, cf.total) for cf in inst.cashflows if cf.date > today]
        if not flows:
            continue
        bonds.append({"price": m.snapshot.price, "flows": flows, "vto": flows[-1][0]})
    return bonds


def _bonds_for_bootstrap_real(metrics_list, indices_provider, settle_date: date) -> list:
    """Real (CER) bootstrap: deflate market price by CER_LIQ-10h / CER_BASE
    so the resulting rate is a real rate."""
    today = date.today()
    target_s = settle_date
    count = 0
    while count < 10:
        target_s -= timedelta(days=1)
        if is_habil(target_s.strftime("%Y-%m-%d")):
            count += 1
    cer_liq = indices_provider.get_cer(target_s)

    bonds = []
    for m in metrics_list:
        inst = m.snapshot.instrument
        if not inst or not m.snapshot.price or not inst.cashflows or not inst.cer_base or not cer_liq:
            continue
        ratio = cer_liq / inst.cer_base
        if ratio <= 0:
            continue
        real_price = m.snapshot.price / ratio
        flows = [(cf.date, cf.total) for cf in inst.cashflows if cf.date > today]
        if not flows:
            continue
        bonds.append({"price": real_price, "flows": flows, "vto": flows[-1][0]})
    return bonds


def _find_pairs(lecap_metrics, cer_metrics, max_days_diff: int = 35):
    today = date.today()
    pairs = []
    lecap_sorted = sorted(
        [m for m in lecap_metrics
         if m.snapshot.instrument and m.snapshot.instrument.maturity_date and m.snapshot.price],
        key=lambda m: m.snapshot.instrument.maturity_date,
    )
    cer_sorted = sorted(
        [m for m in cer_metrics
         if m.snapshot.instrument and m.snapshot.instrument.maturity_date and m.snapshot.price],
        key=lambda m: m.snapshot.instrument.maturity_date,
    )
    used_cer = set()
    for lm in lecap_sorted:
        lec_vto = lm.snapshot.instrument.maturity_date
        if (lec_vto - today).days < 7:
            continue
        best, best_diff = None, max_days_diff + 1
        for cm in cer_sorted:
            if cm.snapshot.instrument.ticker in used_cer:
                continue
            diff = abs((cm.snapshot.instrument.maturity_date - lec_vto).days)
            if diff <= max_days_diff and diff < best_diff:
                best, best_diff = cm, diff
        if best:
            pairs.append((lm, best))
            used_cer.add(best.snapshot.instrument.ticker)
    return pairs



def _dlr_curve_points(today: date):
    rofex = RofexProvider()
    quotes = rofex.get_quotes(ROFEX_SYMBOLS)
    # Spot: política híbrida (mid mayorista en rueda, A3500 BCRA al cierre).
    spot = resolve_spot_for_tna(DolarAPIProvider(), BCRAIndicesProvider())
    if not spot:
        _log_dlr_spot_missing()
        return []
    _reset_dlr_spot_warn()  # spot recuperado — resetear contador de supresión
    points = []
    for sym in ROFEX_SYMBOLS:
        q = quotes.get(sym) or {}
        last = q.get("last") or q.get("settle")
        mat = parse_contract_maturity(sym)
        tna = implied_tna(last, spot, mat, today)
        if tna is None:
            continue
        years = (mat - today).days / 365.25
        if years > 0:
            points.append((years, tna))
    return points



def _append_history(rows: list):
    """Append BEI rows for today. Idempotent: if today's date is already in
    the file, skip. The dedup check scans the tail rather than the whole file
    so it stays O(1) regardless of history length."""
    today_str = date.today().strftime("%Y-%m-%d")
    if os.path.isfile(_HISTORY_CSV):
        try:
            with open(_HISTORY_CSV, "rb") as f:
                # Cheap tail scan: read last ~4 KiB which holds the most
                # recent ~25 rows (1 BEI snapshot = ~7 rows ≈ 1 KiB).
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 4096))
                tail = f.read().decode("utf-8", errors="ignore")
            if any(line.startswith(today_str + ",") for line in tail.splitlines()):
                logger.debug(f"BEI diario para {today_str} ya persistido; salto append.")
                return
        except OSError as e:
            logger.debug(f"History dedup tail-read failed ({e}); appending anyway.")

    os.makedirs(os.path.dirname(_HISTORY_CSV), exist_ok=True)
    write_header = not os.path.isfile(_HISTORY_CSV)
    with open(_HISTORY_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_HISTORY_COLS)
        if write_header:
            w.writeheader()
        for row in rows:
            w.writerow(row)
    logger.info(f"BEI diario apendado a {_HISTORY_CSV} ({len(rows)} filas).")


def _filter_curve_points(points, tir_min=-0.5, tir_max=5.0):
    return [(y, t) for (y, t) in points if y > 0 and tir_min < t < tir_max]


def compute_bei_tables(
    use_case=None,
    indices_provider=None,
    rem_provider=None,
    months_ahead: int = 12,
    persist_history: bool = True,
) -> Optional[dict]:
    """Compute all BEI tables. Returns numeric data; rendering is upstream.

    Output shape:
      {
        "tenor":   [{plazo, dias, tea_nominal, tea_real, tamar_fwd, bei_spot,
                     bei_fwd, bei_g_adj, bei_tamar, dev_implicita, tc_real}, ...],
        "sendero": [{mes, dias_mes, bei_mensual, rem_mensual, diff}, ...],
        "pares":   [{lecap, boncer, vto_lecap, vto_cer, dias, delta_m1,
                     infl_mensual_impl}, ...],
        "meta":    {fits: str, gamma: float|None, rem_12m_yoy: float|None,
                    nom_kind, real_kind, tamar_kind, dlr_kind,
                    nom_n, real_n, tamar_n, dlr_n},
      }

    Numbers are decimal fractions (0.025 = 2.5%). None when unavailable.
    """
    use_case = use_case or build_use_case()
    today = date.today()
    settle_date = settlement_byma(today.strftime("%Y-%m-%d"), lag=1).date()
    indices = indices_provider or BCRAIndicesProvider()
    rem = rem_provider or REMProvider()

    m_tasa_fija = use_case.execute(TASA_FIJA)
    m_tamar = use_case.execute(TAMAR)
    m_dual = use_case.execute(DUAL_TAMAR)
    m_cer = use_case.execute(CER)

    nom_bonds = _bonds_for_bootstrap_nominal(m_tasa_fija, today)
    real_bonds = _bonds_for_bootstrap_real(m_cer, indices, settle_date)
    nom_boot = _filter_curve_points(bootstrap_zero_rates(nom_bonds, today))
    real_boot = _filter_curve_points(bootstrap_zero_rates(real_bonds, today))
    logger.debug(f"Bootstrap: nominal={len(nom_boot)} pts, real={len(real_boot)} pts.")

    nominal_points = list(nom_boot)
    if len(nominal_points) < 4:
        for m in m_tamar + m_dual:
            inst = m.snapshot.instrument
            if m.tir is None or not inst or not inst.maturity_date:
                continue
            years = (inst.maturity_date - today).days / 365.25
            if years > 0 and -0.5 < m.tir < 5.0:
                nominal_points.append((years, m.tir))

    tamar_points = []
    for m in m_tamar + m_dual:
        inst = m.snapshot.instrument
        if m.tir is None or not inst or not inst.maturity_date:
            continue
        years = (inst.maturity_date - today).days / 365.25
        if years > 0 and -0.5 < m.tir < 5.0:
            tamar_points.append((years, m.tir))

    dlr_points = _dlr_curve_points(today)

    nom_curve, nom_kind = _fit_curve(nominal_points, "Curva nominal")
    real_curve, real_kind = _fit_curve(real_boot, "Curva real CER")
    tamar_curve, tamar_kind = _fit_curve(tamar_points, "Curva TAMAR forward")
    dlr_curve, dlr_kind = _fit_curve(dlr_points, "Curva devaluación DLR")

    nom_range = _curve_range(nominal_points)
    real_range = _curve_range(real_boot)
    tamar_range = _curve_range(tamar_points)
    dlr_range = _curve_range(dlr_points)

    if nom_curve is None or real_curve is None:
        logger.error(
            f"Datos insuficientes para BEI base: nominal={len(nominal_points)} "
            f"CER={len(real_boot)} (se requieren ≥2 puntos en cada curva)."
        )
        return None

    gamma, cer_liq_10h, cer_last = _gamma_factor(indices, settle_date)
    rem_12m = rem.get_next_12m_yoy()

    # Tabla 1: tenors estándar
    tenor_rows = []
    history_rows = []
    prev_t = None
    for label, years in _TENORS:
        i_nom = _eval_in_range(nom_curve, years, nom_range)
        r_real = _eval_in_range(real_curve, years, real_range)
        if i_nom is None or r_real is None:
            continue
        bei_spot = fisher_break_even(i_nom, r_real)
        bei_fwd = forward_bei_between_tenors(nom_curve, real_curve, prev_t, years) if prev_t else None
        i_tamar = _eval_in_range(tamar_curve, years, tamar_range)
        bei_tamar = fisher_break_even(i_tamar, r_real) if i_tamar is not None else None
        dev = _eval_in_range(dlr_curve, years, dlr_range)
        tc_drift = real_fx_drift(dev, bei_spot) if dev is not None else None
        bei_lag_adj = (1.0 + bei_spot) / gamma - 1.0 if gamma else None

        tenor_rows.append({
            "plazo": label,
            "dias": int(round(years * 365)),
            "tea_nominal": i_nom,
            "tea_real": r_real,
            "tamar_fwd": i_tamar,
            "bei_spot": bei_spot,
            "bei_fwd": bei_fwd,
            "bei_g_adj": bei_lag_adj,
            "bei_tamar": bei_tamar,
            "dev_implicita": dev,
            "tc_real": tc_drift,
        })
        history_rows.append({
            "fecha": today.strftime("%Y-%m-%d"),
            "tenor_label": label,
            "tenor_years": round(years, 3),
            "tea_nominal": round(i_nom, 6),
            "tea_real": round(r_real, 6),
            "tamar_fwd": round(i_tamar, 6) if i_tamar is not None else None,
            "bei_spot": round(bei_spot, 6),
            "bei_fwd_lag": round(bei_lag_adj, 6) if bei_lag_adj is not None else None,
            "bei_tamar": round(bei_tamar, 6) if bei_tamar is not None else None,
            "dev_implicita": round(dev, 6) if dev is not None else None,
            "tc_real_drift": round(tc_drift, 6) if tc_drift is not None else None,
        })
        prev_t = years

    if persist_history and history_rows:
        _append_history(history_rows)

    # Tabla 2: sendero mensual + REM
    # Fallback para meses sin dato mensual del REM: mensualizar el YoY 12m.
    rem_monthly_fallback = None
    if rem_12m is not None:
        try:
            rem_monthly_fallback = (1.0 + rem_12m) ** (1.0 / 12.0) - 1.0
        except (ValueError, OverflowError):
            rem_monthly_fallback = None

    path = monthly_inflation_path(nom_curve, real_curve, today, months_ahead=months_ahead)
    sendero_rows = []
    for mp in path:
        rem_val = rem.get_for_month(mp.month_start)
        rem_projected = False
        if rem_val is None and rem_monthly_fallback is not None:
            rem_val = rem_monthly_fallback
            rem_projected = True
        diff = (mp.monthly_bei - rem_val) if (mp.monthly_bei is not None and rem_val is not None) else None
        sendero_rows.append({
            "mes": mp.label,
            "dias_mes": mp.days_in_month,
            "bei_mensual": mp.monthly_bei,
            "rem_mensual": rem_val,
            "rem_projected": rem_projected,
            "diff": diff,
        })

    # Tabla 3: pares
    pair_rows = []
    if cer_liq_10h and cer_last:
        for lm, cm in _find_pairs(m_tasa_fija, m_cer):
            lec_inst = lm.snapshot.instrument
            cer_inst = cm.snapshot.instrument
            lec_flows = [cf for cf in lec_inst.cashflows if cf.date > today]
            cer_flows = [cf for cf in cer_inst.cashflows if cf.date > today]
            if not lec_flows or not cer_flows:
                continue
            lec_pay = lec_flows[-1].total
            cer_pp = cer_flows[-1].total
            days = (lec_inst.maturity_date - today).days
            if days <= 0 or not cer_inst.cer_base:
                continue
            delta_m1 = pair_delta(
                lecap_price=lm.snapshot.price,
                lecap_payment_at_maturity=lec_pay,
                cer_price=cm.snapshot.price,
                cer_principal_plus_coupon=cer_pp,
                cer_base=cer_inst.cer_base,
                cer_liq_minus_10h=cer_liq_10h,
                cer_last_known=cer_last,
                days_to_maturity=days,
            )
            monthly = pair_monthly_inflation(
                delta_minus_1=delta_m1, days_covered=days,
            ) if delta_m1 is not None else None
            pair_rows.append({
                "lecap": lec_inst.ticker,
                "boncer": cer_inst.ticker,
                "vto_lecap": lec_inst.maturity_date,
                "vto_cer": cer_inst.maturity_date,
                "dias": days,
                "delta_m1": delta_m1,
                "infl_mensual_impl": monthly,
            })

    meta = {
        "fits": (f"LECAP={nom_kind}(n={len(nominal_points)}) "
                 f"CER={real_kind}(n={len(real_boot)}) "
                 f"TAMAR={tamar_kind or '-'}(n={len(tamar_points)}) "
                 f"DLR={dlr_kind or '-'}(n={len(dlr_points)})"),
        "gamma": gamma,
        "rem_12m_yoy": rem_12m,
        "nom_kind": nom_kind, "real_kind": real_kind,
        "tamar_kind": tamar_kind, "dlr_kind": dlr_kind,
        "nom_n": len(nominal_points), "real_n": len(real_boot),
        "tamar_n": len(tamar_points), "dlr_n": len(dlr_points),
    }
    return {
        "tenor": tenor_rows,
        "sendero": sendero_rows,
        "pares": pair_rows,
        "meta": meta,
    }
