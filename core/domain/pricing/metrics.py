"""Métricas a nivel instrumento, independientes de la strategy: intereses
corridos, valor residual, current yield, DV01, convexidad, PV vanilla.

Extraído de las staticmethods de `FinancialEngine` SIN cambios de fórmula. Las
usan tanto las pricing strategies (`accrued_interest`, `vanilla_pv`) como la
fachada (popup de detalle, tab 3).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from dateutil.relativedelta import relativedelta

# Convención ISMA de stub final: un cupón final cuya duración cae en (MIN, MAX)×período
# regular se extiende a período completo (ver discount_year_fractions). MIN excluye
# "colas" diminutas (artefactos de fecha, ej. AO28 3 días); MAX excluye long-last-coupons.
_STUB_MIN_FRAC = 0.1
_STUB_MAX_FRAC = 0.9


def period_bounds(instrument, ref_date: date) -> Optional[tuple]:
    """(period_start, next_cf) del cupón corriente. None si no hay cupón futuro
    con interés > 0. Maneja soberanos mid-amort cuyo Excel sólo trae flows
    futuros (infiere el período desde freq, no desde emisión)."""
    if not instrument or not instrument.cashflows:
        return None
    cfs = instrument.cashflows  # cronológico (invariante del modelo)
    # Ex-cupón: un flujo que paga EXACTAMENTE en la fecha de liquidación lo cobra el
    # vendedor (el comprador liquida ese día, no es tenedor de registro) → cuenta como
    # PASADO. De ahí el corte estricto: futuros = date > ref, pasados = date <= ref.
    # Para un bono en cualquier otra fecha (sin cupón justo en ref) es idéntico al
    # corte anterior; solo cambia el día del cupón (víspera con liquidación = ese día).
    past = [c for c in cfs if c.date <= ref_date]
    future = [c for c in cfs if c.date > ref_date]
    if not future or future[0].interest <= 0:
        return None
    next_cf = future[0]
    if past:
        return past[-1].date, next_cf
    freq = getattr(instrument, "payment_frequency", 2) or 2
    months_between = max(12 // freq, 1)
    inferred_prev = next_cf.date - relativedelta(months=months_between)
    # Sin cupón pasado → settle está en el PRIMER período: acumula desde la EMISIÓN
    # (cubre 1er cupón regular, CORTO o LARGO — ej. CS50: emis 10/12 → 1er cupón 10/09
    # a 9 meses = 1.5 períodos). Salvaguarda: si la emisión es >2 períodos más vieja
    # que el próximo cupón, es un bono mid-life con flows pasados recortados (soberanos
    # mid-amort) → usar el período regular inferido, NO la emisión.
    emis = instrument.emission_date
    if emis and emis < next_cf.date and emis >= next_cf.date - relativedelta(months=2 * months_between):
        return emis, next_cf
    return inferred_prev, next_cf


def discount_year_fractions(instrument, ref_date: date) -> tuple:
    """(future_cfs, years): fracción de año de descuento ref→cada flujo futuro, con
    la convención del bono — EXCEPTO que un **período final stub** (vto que cae bien
    antes del fin del período regular) se extiende al fin de período regular.

    Es la convención ISMA/Balanz para bonos reestructurados con vto a mitad de período
    (ej. CLISA 2031: último cupón el 12/10/2031 pero el período regular cerraba el
    10/12/2031 → se descuenta al 10/12). Para bonos REGULARES (sin stub) devuelve la
    year_fraction real → idéntico a antes, no cambia ninguna TIR ya verificada.
    """
    cfs = [cf for cf in instrument.cashflows if cf.date > ref_date]  # ex-cupón (ver period_bounds)
    if not cfs:
        return cfs, []
    years = [instrument.year_fraction_to(cf.date, ref_date) for cf in cfs]
    if len(cfs) >= 2 and instrument.maturity_date:
        freq = getattr(instrument, "payment_frequency", 2) or 2
        months = max(12 // freq, 1)
        prev = cfs[-2].date
        regular_end = prev + relativedelta(months=months)
        full = instrument.year_fraction_to(regular_end, prev)
        stub = instrument.year_fraction_to(cfs[-1].date, prev)
        # Extender sólo un cupón final CORTO GENUINO: (a) `cfs[-2]` debe ser un cupón
        # (interest>0) — si fuera amort-only, no marca un período regular y extender
        # sería incorrecto (amortizers multi-fase); (b) la duración del stub entre MIN
        # y MAX del período regular (excluye colas diminutas tipo AO28 y long-last).
        if cfs[-2].interest > 0 and full > 0 and _STUB_MIN_FRAC * full < stub < _STUB_MAX_FRAC * full:
            years[-1] = instrument.year_fraction_to(regular_end, ref_date)
    return cfs, years


def _following_business_day(d: date) -> date:
    """Corre `d` al día hábil siguiente (convención *following*) si cae en finde/
    feriado — la fecha de PAGO real del cupón. Balanz cuenta los intereses corridos
    desde ahí (ej. cupón sáb 17/01/2026 → pagado lun 19/01 → días desde el 19).
    Import function-local del holiday_engine; tope de 10 días por seguridad."""
    from core.holiday_engine import es_habil
    out = d
    for _ in range(10):
        if es_habil(out):
            return out
        out = out + timedelta(days=1)
    return out


def accrued_interest(instrument, ref_date: date) -> float:
    """Intereses corridos per-100-VN, accrual lineal sobre el cupón corriente.
    0 para zero-coupon / capitalizables (LECER, LECAP, BONCER ZC, PURO, DUAL).

    Day-count:
    - 30/360: usa days_30_360 para period_days y elapsed (convención BYMA/Balanz).
    - ACT/365 con cupón pasado disponible: deriva la tasa diaria del cupón anterior
      (robusto ante "long last coupon" donde el next_cf.interest cubre menos días
      que el period_days real).
    - ACT/365 sin cupón pasado (primer período): proratea next_cf sobre period_days.

    Los días corridos se cuentan desde la fecha de PAGO real del cupón anterior (día
    hábil *following* de la fecha programada) — Balanz acumula desde ahí. La tasa
    diaria se deriva del período PROGRAMADO (= tasa anual/365), no del corrido."""
    bounds = period_bounds(instrument, ref_date)
    if not bounds:
        return 0.0
    period_start, next_cf = bounds
    # Fecha de pago real (día hábil) del cupón anterior → desde ahí corren los días.
    eff_start = _following_business_day(period_start)
    elapsed = (ref_date - eff_start).days
    if elapsed <= 0:
        return 0.0

    if instrument.is_30_360:
        from core.domain.cashflow_synth import days_30_360 as _d30360
        period_days = _d30360(eff_start, next_cf.date)
        elapsed_dc = min(_d30360(eff_start, ref_date), period_days)
        if period_days <= 0:
            return 0.0
        return next_cf.interest * elapsed_dc / period_days

    # ACT/365: preferir tasa diaria derivada del cupón pasado para manejar
    # correctamente bonds con "long last coupon" (vto ≠ fecha regular del último cupón).
    # OJO: la tasa diaria usa el período PROGRAMADO (prev_cf.date − prev_start) → es
    # tasa_anual/365, independiente del corrimiento a día hábil del pago.
    cfs = instrument.cashflows  # cronológico (invariante del modelo)
    past_int = [c for c in cfs if c.date < ref_date and c.interest > 0]
    if past_int:
        prev_cf = past_int[-1]
        idx = next((i for i, c in enumerate(cfs) if c.date == prev_cf.date), None)
        if idx is not None:
            prev_start = cfs[idx - 1].date if idx > 0 else (instrument.emission_date or prev_cf.date)
            past_period = (prev_cf.date - prev_start).days
            if past_period > 0:
                daily_rate = prev_cf.interest / past_period
                max_elapsed = (next_cf.date - eff_start).days
                return daily_rate * min(elapsed, max_elapsed)

    # Primer período (sin cupón pasado): proratea next_cf sobre el período real.
    period_days = (next_cf.date - eff_start).days
    if period_days <= 0:
        return 0.0
    return next_cf.interest * min(elapsed, period_days) / period_days


def days_since_last_coupon(instrument, ref_date: date) -> Optional[int]:
    """Días desde la fecha de PAGO real del último cupón = día hábil *following* del
    corte programado (el mismo inicio que usa `accrued_interest`), para que el label
    '(N días)' quede consistente con el monto corrido. Desde emisión si nunca pagó."""
    bounds = period_bounds(instrument, ref_date)
    if not bounds:
        return None
    return max(0, (ref_date - _following_business_day(bounds[0])).days)


def residual_nominal(instrument, ref_date: date) -> float:
    """VR (Valor Residual) per-100 = suma de amortizaciones futuras.
    Fallback a 100 − amortizado para schedules incompletos."""
    if not instrument or not instrument.cashflows:
        return 100.0
    past = [c for c in instrument.cashflows if c.date <= ref_date]
    future = [c for c in instrument.cashflows if c.date > ref_date]  # ex-cupón
    residual = sum(c.amortization for c in future)
    if residual <= 0:
        amortized = sum(c.amortization for c in past)
        residual = max(100.0 - amortized, 0.0)
    return residual


def current_yield(instrument, price_dirty: float, ref_date: date) -> Optional[float]:
    """Current yield (decimal) = cupón anual nominal (por 100 de VR vivo) / precio CLEAN.

    Convención IAMC/Balanz. El cupón anual se recupera del próximo cupón vigente
    normalizando por la fracción de año de su período (`interest / dcf`) → es la tasa
    de cupón × residual, exacta aun con semestres de 181/184 días, con un solo cupón
    en el año, o con stub long-last. Denominador CLEAN (dirty − corridos), no dirty.
    None para zero-coupon/capitalizables (sin cupón futuro con interés > 0).

    Antes calculaba Σ(intereses de los próximos 12m) / dirty: eso subestimaba ~a la
    mitad los bonos con un solo cupón futuro dentro de la ventana de 12m (ONs a <1 año)
    y sesgaba bajo por usar el dirty. Es métrica de DISPLAY (no alimenta TIR/VT/MD)."""
    if not instrument or not instrument.cashflows or not price_dirty or price_dirty <= 0:
        return None
    bounds = period_bounds(instrument, ref_date)
    if not bounds:
        return None
    period_start, next_cf = bounds
    if next_cf.interest <= 0:
        return None
    # Cupón anual = tasa de cupón × residual, recuperada del próximo cupón normalizando
    # por la fracción de año del período (convención del bono): annual = interest / dcf.
    # Robusto al day-count real (semestres de 181/184 días), a que quede UN solo cupón
    # en el año (no subestima) y a cupones long-last con stub (recupera la tasa real).
    dcf = instrument.year_fraction_to(next_cf.date, period_start)
    if dcf and dcf > 0:
        annual_coupon = next_cf.interest / dcf
    else:
        freq = getattr(instrument, "payment_frequency", 2) or 2
        annual_coupon = next_cf.interest * freq
    clean = price_dirty - accrued_interest(instrument, ref_date)
    if clean <= 0:
        return None
    return annual_coupon / clean


def vanilla_pv(instrument, tir: float, ref_date: date) -> Optional[float]:
    """PV de los flujos futuros descontados al tir con la convención declarada del
    instrumento (ACT/365 ONs, ACT/365.25 soberanos, 30/360, ACT/ACT). None si no
    hay flujos. Guard de overflow ante TIR degenerada."""
    if not instrument or tir is None or tir <= -1.0:
        return None
    future, years = discount_year_fractions(instrument, ref_date)
    if not future:
        return None
    try:
        pv = 0.0
        for cf, t in zip(future, years):
            if t <= 0:
                continue
            pv += cf.total / (1.0 + tir) ** t
    except (OverflowError, ZeroDivisionError, ValueError):
        return None
    return pv if pv > 0 else None


def dv01(instrument, tir: float, ref_date: date) -> Optional[float]:
    """Cambio de precio per-100 ante -1bp en TIR. ΔP = P(tir-1bp) − P(tir)."""
    if instrument is None or tir is None:
        return None
    p0 = vanilla_pv(instrument, tir, ref_date)
    p1 = vanilla_pv(instrument, tir - 0.0001, ref_date)
    if p0 is None or p1 is None:
        return None
    return p1 - p0


def convexity(instrument, tir: float, ref_date: date) -> Optional[float]:
    """Convexidad en años². C = (1/P) × Σ cf × t × (t+1) / (1+r)^(t+2)."""
    if instrument is None or tir is None or tir <= -1.0:
        return None
    future, years = discount_year_fractions(instrument, ref_date)
    if not future:
        return None
    pv = 0.0
    weighted = 0.0
    for cf, t in zip(future, years):
        if t <= 0:
            continue
        pv += cf.total / (1.0 + tir) ** t
        weighted += cf.total * t * (t + 1.0) / ((1.0 + tir) ** (t + 2.0))
    if pv <= 0:
        return None
    return weighted / pv
