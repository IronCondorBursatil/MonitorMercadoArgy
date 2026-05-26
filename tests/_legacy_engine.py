import threading
from datetime import date, timedelta
from typing import List, Optional

import numpy as np
from dateutil.relativedelta import relativedelta
from scipy.optimize import brentq, newton

from core.domain.models import MarketSnapshot
from core.holiday_engine import is_habil, settlement_byma

# XIRR Newton seeds — span typical sovereign/corporate yields in AR.
_XIRR_GUESSES   = (0.05, 0.20, -0.10, 0.80, -0.50)
_XIRR_TOLERANCE = 1e-4
_PCT_CHANGE_EPS = 1e-12

# Year length for act/365.25 TIR convention (Julian calendar average).
_JULIAN_YEAR = 365.25


def _is_cer_type(instrument_type: str) -> bool:
    # CER-adjusted bonds in the master Excel use several `tipo` values that
    # don't all contain the substring "CER" (DICP/CUAP are "CON CUPON";
    # PARP is "STEP-UP"). All belong to the CER sheet — match the union.
    # Exclude TAMAR variants (DUAL_CER_TAMAR contains "CER" but is not a CER
    # bond — it's a Dual that pays max(CER, TAMAR) and lives in the TAMAR universe).
    itype = instrument_type.upper().strip()
    if "TAMAR" in itype:
        return False
    return any(token in itype for token in ("CER", "CON CUPON", "STEP-UP"))


def _is_bopreal_type(instrument_type: str) -> bool:
    return "BOPREAL" in instrument_type.upper().strip()


def _is_30_360(instrument) -> bool:
    """True si el instrumento usa base 30/360 para el cálculo de TIR y duración.
    Detecta el campo `day_count` del modelo; fallback por tipo para BOPREAL."""
    dc = getattr(instrument, "day_count", "") or ""
    if "30/360" in dc:
        return True
    return _is_bopreal_type(getattr(instrument, "instrument_type", ""))


def _xirr_from_years(flows: np.ndarray, years: np.ndarray) -> float:
    """XIRR con fracciones de año pre-calculadas (cualquier day-count)."""
    def npv(rate):
        if rate <= -1.0:
            return 1e12
        return np.sum(flows / (1 + rate) ** years)
    for guess in _XIRR_GUESSES:
        try:
            res = newton(npv, guess, maxiter=50)
            if not np.isnan(res) and abs(npv(res)) < _XIRR_TOLERANCE:
                return res
        except (RuntimeError, ValueError, OverflowError):
            continue
    try:
        return brentq(npv, -0.999, 10.0)
    except (RuntimeError, ValueError):
        return np.nan


def _is_dolar_linked_type(instrument_type: str) -> bool:
    return "DOLAR_LINKED" in instrument_type or "DOLAR LINKED" in instrument_type


def _is_tamar_puro_type(instrument_type: str) -> bool:
    return instrument_type.upper().strip() == "PURO"


def _is_dual_tamar_type(instrument_type: str) -> bool:
    return instrument_type.upper().strip() == "DUAL"


def _is_dual_cer_tamar_type(instrument_type: str) -> bool:
    """TXMJ* series: bullet bond paying max(CER+spread, TAMAR+spread) at maturity."""
    return instrument_type.upper().strip() == "DUAL_CER_TAMAR"


def _project_cer_at(target_date: date, indices_provider) -> Optional[float]:
    """Linear extrapolation of CER index to a future date using the last 30 days
    of observed CER growth. For TXMJ* duals which mature 2-3 years out, this is
    a coarse approximation — but the bond's TIR is bounded by the TAMAR rail
    anyway because the holder receives max(CER, TAMAR) at maturity.
    """
    today = date.today()
    cer_today = indices_provider.get_cer(today)
    cer_30_ago = indices_provider.get_cer(today - timedelta(days=30))
    if not cer_today or not cer_30_ago or cer_30_ago <= 0:
        return cer_today
    if target_date <= today:
        return indices_provider.get_cer(target_date)
    monthly_growth = cer_today / cer_30_ago - 1.0
    months_ahead = (target_date - today).days / 30.0
    return cer_today * (1.0 + monthly_growth) ** months_ahead


# --------------------------------------------------------------------------- #
# Fórmula oficial BONTE TAMAR (Comunicación BCRA / condiciones de emisión)
# Capitalización MENSUAL con day-count 30/360. TAMAR_TEM se computa con:
#     TAMAR_TEM = ((1 + TNA/k)^k)^(1/12) − 1     donde k = 365/32 ≈ 11.40625
# Pago a vencimiento = VNO × (1 + TEM_max)^N_meses
# Para DUAL: TEM_max = max(TAMAR_TEM, fixed_TEM_mensual)
# Para DUAL_CER_TAMAR: max(rail TAMAR monthly, rail CER ratio × (1+spread)^years)
# Validado contra calculadora Balanz/IAMC para TTJ26 a precio 158.20 (V.Téc
# 146.39, payback 164.32, TIR_EA 39.06%).
# --------------------------------------------------------------------------- #

_TAMAR_K = 365.0 / 32.0  # ≈ 11.40625 (períodos de 32 días por año en la fórmula oficial)


# Importar desde cashflow_synth — única fuente de verdad del day-count 30/360.
# La copia local ha sido eliminada para evitar divergencias.
from core.domain.cashflow_synth import days_30_360 as _days_30_360


def _tamar_tem(tna_dec: Optional[float]) -> Optional[float]:
    """TAMAR (TNA decimal) → TEM (decimal) via fórmula oficial BONTE TAMAR.

    `((1 + TNA/k)^k)^(1/12) − 1` con k = 365/32. Equivalente a interpretar
    TAMAR como una tasa nominal con capitalización en períodos de 32 días.
    """
    if tna_dec is None:
        return None
    try:
        return ((1.0 + tna_dec / _TAMAR_K) ** _TAMAR_K) ** (1.0 / 12.0) - 1.0
    except (ValueError, OverflowError, ZeroDivisionError):
        return None


# Cache del avg TAMAR (decimal) por (emission, end, forecast_tna). Invalidado
# al cambiar el día calendario — la serie BCRA TAMAR no se mueve intraday y
# el avg es estable hasta mañana. Sin esto, cada ciclo de refresh (5s) gasta
# ~3500 dict-lookups recomputando el mismo avg para 10 bonos TAMAR.
_AVG_TAMAR_CACHE: dict = {}
_AVG_TAMAR_DAY: Optional[date] = None
_AVG_TAMAR_LOCK = threading.Lock()


def _avg_tamar_tna(
    period_start: date, period_end: date, indices_provider,
    forecast_tna: Optional[float] = None,
) -> Optional[float]:
    """Promedio aritmético simple de TAMAR (TNA decimal) sobre [start, end].

    - Para días <= today: TAMAR observada de BCRA (lookup con fallback hábil).
    - Para días > today: `forecast_tna` (decimal). Si None, usa la última
      TAMAR observada como projection.

    El documento de condiciones especifica el promedio sobre el período
    `[emission − 10 BD, maturity − 10 BD]`. Acá lo simplificamos a las fechas
    exactas — el impacto del lag de 10 BD es < 0.5% sobre el avg.

    Cacheado por (start, end, forecast) con invalidación diaria (TAMAR no se
    mueve intraday). Hit típico ~99% en el dashboard.
    """
    global _AVG_TAMAR_DAY
    if period_end <= period_start:
        return None
    today = date.today()
    key = (period_start, period_end, forecast_tna)
    with _AVG_TAMAR_LOCK:
        if _AVG_TAMAR_DAY != today:
            _AVG_TAMAR_CACHE.clear()
            _AVG_TAMAR_DAY = today
        cached = _AVG_TAMAR_CACHE.get(key)
        if cached is not None or key in _AVG_TAMAR_CACHE:
            return cached

    # Past portion: sumar TAMAR observada día por día sobre [start, min(today, end)].
    # `get_tamar` ya hace forward-fill para fines de semana/feriados.
    cache = getattr(indices_provider, "_cache_tamar", None) or {}
    past_end = min(today, period_end)
    past_sum, past_n = 0.0, 0
    if past_end >= period_start:
        d = period_start
        one_day = timedelta(days=1)
        while d <= past_end:
            t_pct = indices_provider.get_tamar(d)
            if t_pct is not None:
                past_sum += t_pct
                past_n += 1
            d += one_day
        past_sum /= 100.0  # convert TNA% to decimal once, fuera del loop

    # Future portion: forecast × N_días, sin loop.
    future_days = max(0, (period_end - max(today, period_start - timedelta(days=1))).days)
    if forecast_tna is not None:
        future_tna = forecast_tna
    elif cache:
        past_dates = [d for d in cache.keys() if d <= today]
        if past_dates:
            future_tna = cache[max(past_dates)] / 100.0
        else:
            future_tna = None
    else:
        future_tna = None
    future_sum = (future_tna * future_days) if (future_tna is not None and future_days) else 0.0
    future_n = future_days if future_tna is not None else 0

    n = past_n + future_n
    result = (past_sum + future_sum) / n if n > 0 else None

    with _AVG_TAMAR_LOCK:
        _AVG_TAMAR_CACHE[key] = result
    return result


def _tamar_dual_payoff_at(
    instrument, ref_date: date, indices_provider,
    *, tamar_forecast: Optional[float] = None, to_date: Optional[date] = None,
) -> Optional[float]:
    """Valor per-100 del bono TAMAR PURO/DUAL/DUAL_CER_TAMAR a fecha `to_date`
    (default = maturity). Si `to_date == ref_date`, devuelve V.Téc al settle.
    Si `to_date == maturity`, devuelve el payoff proyectado.

    Algoritmo (fórmula oficial del documento BONTE TAMAR):
      1. avg_TAMAR = promedio aritmético simple TAMAR observada (BCRA) sobre
         [emission, end]; para días futuros usa `tamar_forecast` (decimal) o
         fallback a la última TAMAR observada.
      2. TEM_tamar = _tamar_tem(avg_TAMAR + spread). Aplica spread al TNA antes
         de convertir vía `((1+T/k)^k)^(1/12) − 1` con k=365/32.
      3. Para DUAL con floor: TEM_max = max(TEM_tamar, floor_TEM_mensual).
      4. N_meses = days_30_360(emission, end) / 30.
      5. Valor = 100 × (1 + TEM_max)^N_meses.

    Para DUAL_CER_TAMAR adicionalmente computa el rail CER y devuelve max.

    Nota: el doc oficial dice "promedio aritmético simple"; la calculadora
    Balanz/IAMC en la práctica fixea TAMAR mes a mes, lo que da resultados
    distintos. La diferencia se cubre permitiendo al usuario sobrescribir el
    `tamar_forecast` (campo "TAMAR proyectado" en la calculadora del popup).
    """
    if instrument is None or indices_provider is None:
        return None
    if not instrument.emission_date:
        return None
    end = to_date if to_date is not None else instrument.maturity_date
    if not end or end <= instrument.emission_date:
        return None
    itype = (instrument.instrument_type or "").upper().strip()

    spread = instrument.spread_rate or 0.0
    avg_t = _avg_tamar_tna(instrument.emission_date, end, indices_provider,
                            forecast_tna=tamar_forecast)
    if avg_t is None:
        return None
    tem_tamar = _tamar_tem(avg_t + spread)
    if tem_tamar is None:
        return None

    # DUAL: max contra floor mensual.
    if _is_dual_tamar_type(itype) and instrument.floor_rate_monthly is not None:
        tem_max = max(tem_tamar, instrument.floor_rate_monthly)
    else:
        tem_max = tem_tamar

    n_months = _days_30_360(instrument.emission_date, end) / 30.0
    try:
        payoff_tamar = 100.0 * (1.0 + tem_max) ** n_months
    except (ValueError, OverflowError):
        return None

    # DUAL_CER_TAMAR: comparar contra rail CER al vencimiento.
    if _is_dual_cer_tamar_type(itype) and instrument.cer_base and instrument.cer_base > 0:
        cer_spread = instrument.cer_spread or 0.0
        cer_at_end = _project_cer_at(end, indices_provider)
        if cer_at_end:
            years = (end - instrument.emission_date).days / _JULIAN_YEAR
            payoff_cer = 100.0 * (cer_at_end / instrument.cer_base) * (1.0 + cer_spread) ** years
            return max(payoff_tamar, payoff_cer)

    return payoff_tamar


# Cache `_settlement_for` por (lag, today). Sólo hay 2 valores posibles por
# día (T+0 y T+1) pero `settlement_byma` toma ~2ms (holiday calendar lookup),
# así que sin cache cada cálculo de TIR/V.Téc sumaba 2ms × N_bonds × ciclos.
# Invalidación: el día calendario.
_SETTLE_CACHE: dict = {}     # {lag: settle_date}
_SETTLE_CACHE_DAY: Optional[date] = None
_SETTLE_CACHE_LOCK = threading.Lock()


def _settlement_for(instrument_type: str) -> date:
    lag = 1
    today = date.today()
    with _SETTLE_CACHE_LOCK:
        global _SETTLE_CACHE_DAY
        if _SETTLE_CACHE_DAY != today:
            _SETTLE_CACHE.clear()
            _SETTLE_CACHE_DAY = today
        cached = _SETTLE_CACHE.get(lag)
        if cached is not None:
            return cached
    # settlement_byma() toma ~2ms (holiday calendar lookup) — fuera del lock.
    settle = settlement_byma(today.strftime("%Y-%m-%d"), lag=lag).date()
    with _SETTLE_CACHE_LOCK:
        _SETTLE_CACHE[lag] = settle
    return settle


def _resolve_settle(instrument_type: str, override: Optional[date]) -> date:
    """Use the override if provided, else default per instrument type. La
    calculadora del popup expone un toggle T+0/T+1 que pasa el override; el
    refresh loop normal sigue usando la convención por tipo."""
    return override if override is not None else _settlement_for(instrument_type)


def _cer_reference_date(settle: date, lag_business_days: int) -> date:
    target = settle
    count = 0
    while count < lag_business_days:
        target -= timedelta(days=1)
        if is_habil(target.strftime("%Y-%m-%d")):
            count += 1
    return target


class FinancialEngine:
    @staticmethod
    def xirr(flows: List[float], dates: List[date]) -> float:
        if not flows or len(flows) < 2:
            return np.nan
        d0 = dates[0]
        years = np.array([(d - d0).days / _JULIAN_YEAR for d in dates])
        return _xirr_from_years(np.array(flows), years)

    @staticmethod
    def calculate_technical_value(snapshot: MarketSnapshot, indices_provider, fx_provider=None,
                                  ref_date: Optional[date] = None) -> float:
        """Valor Técnico (Valor Par) = residual nominal + accrued interest.

        Universal formula matching the BYMA/IAMC calculator convention used
        by Balanz/Rava: at any reference date, the technical value of the bond
        per-100-VN equals the still-outstanding principal plus the linearly-
        accrued portion of the next coupon.

        For CER-adjusted bonds, the result is then multiplied by CER_LIQ-10h /
        CER_BASE (BCRA NT N°8/2024).

        For DOLAR LINKED bonds, V.Téc is expressed in pesos = residual USD ×
        mayorista venta. The bond pays 100 USD at maturity; price is quoted in
        pesos, so paridad = price_pesos / (100 × FX). `fx_provider` is required
        for DL bonds; without it the function falls back to 100.

        Returns 100.0 only when there's not enough info (no cashflows, no past
        flows AND no emission date) — degenerate fallback.
        """
        inst = snapshot.instrument
        if not inst:
            return 100.0

        ref = ref_date if ref_date is not None else date.today()

        # DOLAR LINKED: par face = 100 USD. V.Téc in pesos = residual_USD × FX.
        # Residual = sum(future amortizations) (per-100 convention); falls back
        # to 100 for pure zero-coupon DL bonds that have no Excel cashflows.
        if _is_dolar_linked_type(inst.instrument_type) and fx_provider:
            fx = fx_provider.get_mayorista_venta()
            if fx and fx > 0:
                future_cfs = [cf for cf in (inst.cashflows or []) if cf.date >= ref]
                residual_usd = sum(cf.amortization for cf in future_cfs)
                if residual_usd <= 0:
                    residual_usd = 100.0
                return residual_usd * fx
            return 100.0

        # TAMAR PURO/DUAL: V.Téc(t) computado vía fórmula oficial BONTE TAMAR.
        if (_is_tamar_puro_type(inst.instrument_type)
                or _is_dual_tamar_type(inst.instrument_type)):
            if indices_provider and inst.emission_date and inst.emission_date < ref:
                v = _tamar_dual_payoff_at(inst, ref, indices_provider, to_date=ref)
                if v is not None:
                    return v
            return 100.0

        # DUAL_CER_TAMAR (TXMJ series): VT como bono CER ZC — 100 × CER_ref / cer_base.
        if _is_dual_cer_tamar_type(inst.instrument_type) and indices_provider and inst.cer_base:
            settle = settlement_byma(ref.strftime("%Y-%m-%d"), lag=1).date()
            target_date = _cer_reference_date(settle, inst.cer_lag)
            cer_val = indices_provider.get_cer(target_date)
            if cer_val:
                return 100.0 * cer_val / inst.cer_base
            return 100.0

        all_cfs = sorted(inst.cashflows or [], key=lambda cf: cf.date)
        past_cfs = [cf for cf in all_cfs if cf.date < ref]
        future_cfs = [cf for cf in all_cfs if cf.date >= ref]

        # Single-flow capitalizable bond (LECAP, BONCAP, etc.): V.Téc grows
        # geometrically from 100 at emission to the payoff at maturity. Today's
        # V.Téc is 100 × (payoff/100)^(elapsed_days/total_days), matching the
        # Argentine market convention for capitalizable letters.
        if (len(future_cfs) == 1 and not past_cfs
                and future_cfs[0].interest == 0
                and inst.emission_date and inst.emission_date < ref):
            payoff = future_cfs[0].amortization
            total = (future_cfs[0].date - inst.emission_date).days
            elapsed = (ref - inst.emission_date).days
            if total > 0 and payoff > 0 and 0 < elapsed <= total:
                base_value = 100.0 * (payoff / 100.0) ** (elapsed / total)
                if _is_cer_type(inst.instrument_type) and indices_provider and inst.cer_base:
                    settle = settlement_byma(ref.strftime("%Y-%m-%d"), lag=1).date()
                    target_date = _cer_reference_date(settle, inst.cer_lag)
                    cer_val = indices_provider.get_cer(target_date)
                    if cer_val:
                        return base_value * cer_val / inst.cer_base
                return base_value

        # Residual nominal in BASE terms (before CER indexation):
        # Sum of remaining amortizations. The Excel typically stores only
        # future flows for sovereigns mid-amortization (AL29D etc.), so
        # 100 − past_amort would give 100 (wrong). Sum of future amort
        # works in both cases provided the bond pays back 100 total.
        residual = sum(cf.amortization for cf in future_cfs)
        if residual <= 0:
            # Past-maturity (no future flows) or a flow schedule that does not
            # follow the per-100 convention — fall back to (100 − past_amort).
            amortized = sum(cf.amortization for cf in past_cfs)
            residual = max(100.0 - amortized, 0.0)

        # Accrued lineal sobre el cupón corriente. Reusa _period_bounds que
        # maneja correctamente el caso "soberano mid-amort sin past flows en
        # Excel" (inferir período desde freq, no desde emisión).
        accrued = FinancialEngine.accrued_interest(inst, ref)
        base_value = residual + accrued

        # CER indexation factor (NT N°8/2024 Eq. 13).
        if _is_cer_type(inst.instrument_type) and indices_provider and inst.cer_base:
            settle = settlement_byma(ref.strftime("%Y-%m-%d"), lag=1).date()
            target_date = _cer_reference_date(settle, inst.cer_lag)
            cer_val = indices_provider.get_cer(target_date)
            if cer_val:
                # Bonds con capital_factor > 1 (bonos reestructurados: DICP/DIP0/CUAP)
                # tienen cashflows escalados en nominal_initial = 100 × capital_factor.
                # Normalizar a base-100 dividiendo por total_amort / 100 antes de
                # aplicar el ratio CER — de lo contrario el VT queda inflado por capital_factor.
                total_amort_all = sum(cf.amortization for cf in all_cfs)
                if total_amort_all > 0.01:
                    base_value = base_value * 100.0 / total_amort_all
                return base_value * cer_val / inst.cer_base

        return base_value

    @staticmethod
    def calculate_tir(
        snapshot: MarketSnapshot,
        indices_provider=None,
        fx_provider=None,
        settle_date: Optional[date] = None,
        tamar_forecast: Optional[float] = None,
    ) -> Optional[float]:
        """Internal Rate of Return (TIR) as a decimal fraction (0.30 = 30%).

        For CER-indexed bonds, computes the REAL TIR per BCRA Nota Técnica
        N°8/2024 Eq. A7: price is deflated by CER_LIQ-10h / CER_BASE and IRR
        is solved against the nominal-base cashflows (per-100 nominal).

        For DOLAR_LINKED bonds, computes the USD TIR: price is deflated by
        the mayorista venta rate (pesos/USD) to express today's investment in
        USD, then solved against USD-100 payback at maturity.

        Requires Excel `Cashflows` to be stored in base terms — see agents.md.
        """
        inst = snapshot.instrument
        if not inst or not snapshot.price:
            return None

        settle_date = _resolve_settle(inst.instrument_type, settle_date)

        # TAMAR PURO/DUAL/DUAL_CER_TAMAR: capitalización mensual con fórmula
        # oficial BONTE TAMAR. Payback a vto = 100 × (1+TEM_max)^N_meses,
        # donde TEM_max = max(_tamar_tem(avg_TAMAR + spread), floor_TEM_mensual).
        # `tamar_forecast` (decimal) sobrescribe la TAMAR forward usada para
        # promediar la parte futura de [emission, maturity]. Default = última
        # publicada por BCRA.
        #
        # OPTIMIZACIÓN: TAMAR bullets tienen 1 sólo flow → TIR cerrada,
        # `tir = (payback/price)^(1/years) − 1`. Salta el Newton de xirr
        # (~10-25x más rápido en este branch).
        if (_is_tamar_puro_type(inst.instrument_type)
                or _is_dual_tamar_type(inst.instrument_type)) \
                and indices_provider and inst.emission_date \
                and inst.maturity_date and inst.maturity_date > settle_date:
            expected_payback = _tamar_dual_payoff_at(
                inst, settle_date, indices_provider,
                tamar_forecast=tamar_forecast, to_date=inst.maturity_date,
            )
            if expected_payback is None or expected_payback <= 0:
                return None
            years = (inst.maturity_date - settle_date).days / _JULIAN_YEAR
            if years <= 0 or snapshot.price <= 0:
                return None
            try:
                return (expected_payback / snapshot.price) ** (1.0 / years) - 1.0
            except (ValueError, OverflowError, ZeroDivisionError):
                return None

        # DUAL_CER_TAMAR (TXMJ series): TIR real como bono CER ZC.
        # real_price = price / (CER_ref / cer_base); TIR = (100/real_price)^(1/years) − 1.
        if _is_dual_cer_tamar_type(inst.instrument_type) and indices_provider and inst.cer_base:
            if inst.maturity_date and inst.maturity_date > settle_date:
                target_s = _cer_reference_date(settle_date, inst.cer_lag)
                cer_s = indices_provider.get_cer(target_s)
                if cer_s:
                    real_price = snapshot.price / (cer_s / inst.cer_base)
                    years = (inst.maturity_date - settle_date).days / _JULIAN_YEAR
                    if years > 0 and real_price > 0:
                        try:
                            return (100.0 / real_price) ** (1.0 / years) - 1.0
                        except (ValueError, OverflowError, ZeroDivisionError):
                            pass
            return None

        # USD TIR for DOLAR LINKED bonds
        if _is_dolar_linked_type(inst.instrument_type) and fx_provider:
            fx = fx_provider.get_mayorista_venta()
            if fx and fx > 0 and inst.maturity_date and inst.maturity_date > settle_date:
                real_price_usd = snapshot.price / fx
                flows = [-real_price_usd, 100.0]
                dates = [settle_date, inst.maturity_date]
                tir = FinancialEngine.xirr(flows, dates)
                return float(tir) if not np.isnan(tir) else None
            return None

        future_cfs = inst.get_future_cashflows(settle_date)
        if not future_cfs:
            return None

        # Real TIR for CER bonds: deflate price by CER ratio.
        if _is_cer_type(inst.instrument_type) and indices_provider and inst.cer_base:
            target_s = _cer_reference_date(settle_date, inst.cer_lag)
            cer_s = indices_provider.get_cer(target_s)
            if cer_s:
                real_price = snapshot.price / (cer_s / inst.cer_base)
                flows = [-real_price] + [cf.total for cf in future_cfs]
                dates = [settle_date] + [cf.date for cf in future_cfs]
                tir = FinancialEngine.xirr(flows, dates)
                return float(tir) if not np.isnan(tir) else None

        # Bonos con base 30/360 (BOPREAL y cualquier instrumento marcado en Excel).
        # El XIRR se resuelve con fracciones de año 30/360 en lugar de Act/365.25.
        if _is_30_360(inst):
            flows_arr = np.array([-snapshot.price] + [cf.total for cf in future_cfs])
            years_arr = np.array(
                [0.0] + [_days_30_360(settle_date, cf.date) / 360.0 for cf in future_cfs]
            )
            tir = _xirr_from_years(flows_arr, years_arr)
            return float(tir) if not np.isnan(tir) else None

        flows = [-snapshot.price] + [cf.total for cf in future_cfs]
        dates = [settle_date] + [cf.date for cf in future_cfs]
        tir = FinancialEngine.xirr(flows, dates)
        return float(tir) if not np.isnan(tir) else None

    @staticmethod
    def calculate_duration(snapshot: MarketSnapshot, tir: float,
                           settle_date: Optional[date] = None) -> Optional[float]:
        """Modified Duration following the BYMA/IAMC convention used by local
        bond calculators (Balanz, IAMC, Rava): MD = Macaulay / (1+TEA)^(1/m),
        where m is the bond's annual payment frequency.

        For zero-coupon / bullet bonds (single flow), m=1 and the formula
        collapses to the standard MD = Macaulay / (1+TEA).
        """
        inst = snapshot.instrument
        if not inst or tir is None or np.isnan(tir):
            return None

        settle_date = _resolve_settle(inst.instrument_type, settle_date)

        # Bullet bonds (single payment at maturity): DL, TAMAR PURO, DUAL TAMAR,
        # DUAL CER/TAMAR. TAMAR-family bonds capitalize monthly → m=12;
        # DL is an annual single payment → m=1.
        is_bullet = (
            _is_dolar_linked_type(inst.instrument_type)
            or _is_tamar_puro_type(inst.instrument_type)
            or _is_dual_tamar_type(inst.instrument_type)
            or _is_dual_cer_tamar_type(inst.instrument_type)
        )
        if is_bullet and inst.maturity_date and inst.maturity_date > settle_date:
            years = (inst.maturity_date - settle_date).days / _JULIAN_YEAR
            # TAMAR: monthly compounding → m=12. DL and DUAL_CER_TAMAR: annual → m=1.
            m_bullet = 12 if (
                _is_tamar_puro_type(inst.instrument_type)
                or _is_dual_tamar_type(inst.instrument_type)
            ) else 1
            return years / (1 + tir) ** (1.0 / m_bullet)

        future_cfs = inst.get_future_cashflows(settle_date)
        if not future_cfs:
            return None

        # Bonos base 30/360: devuelve Duración Macaulay con day-count 30/360.
        # La referencia del mercado (Data912 / Balanz) reporta MacaulayD para
        # BOPREAL, no Modified Duration — lo que coincide con la convención de
        # compounding continuo (MacaulayD = MD bajo compounding continuo).
        if _is_30_360(inst):
            total_pv, weighted_pv = 0.0, 0.0
            for cf in future_cfs:
                t = _days_30_360(settle_date, cf.date) / 360.0
                pv = cf.total / (1 + tir) ** t
                total_pv += pv
                weighted_pv += pv * t
            return (weighted_pv / total_pv) if total_pv > 0 else None

        total_pv = 0.0
        weighted_pv = 0.0
        for cf in future_cfs:
            t = (cf.date - settle_date).days / _JULIAN_YEAR
            pv = cf.total / (1 + tir) ** t
            total_pv += pv
            weighted_pv += pv * t

        if total_pv <= 0:
            return None
        macaulay = weighted_pv / total_pv
        # Use bond's payment frequency. Single-flow / unknown freq → m=1 (TEA-based).
        freq = getattr(inst, "payment_frequency", 1) or 1
        if len(future_cfs) <= 1:
            freq = 1
        return macaulay / (1 + tir) ** (1.0 / freq)

    @staticmethod
    def calculate_theoretical_price(
        instrument, tir: float, reference_date: date
    ) -> Optional[float]:
        """Price implied by discounting future cashflows at the given TIR (decimal fraction)."""
        if instrument is None or tir is None:
            return None
        future = instrument.get_future_cashflows(reference_date)
        if not future:
            return None
        price = 0.0
        for cf in future:
            years = (cf.date - reference_date).days / _JULIAN_YEAR
            if years <= 0:
                continue
            price += cf.total / (1 + tir) ** years
        return price if price > 0 else None

    @staticmethod
    def calculate_pct_change(
        current: Optional[float], previous: Optional[float]
    ) -> Optional[float]:
        if current is None or previous is None or abs(previous) < _PCT_CHANGE_EPS:
            return None
        return (current - previous) / previous

    @staticmethod
    def projected_payoff(instrument, indices_provider,
                         tamar_forecast: Optional[float] = None,
                         ref_date: Optional[date] = None) -> Optional[float]:
        """Payback proyectado per-100 a vencimiento para bonos TAMAR-family
        (PURO/DUAL/DUAL_CER_TAMAR). Usa la fórmula oficial BONTE TAMAR
        (capitalización mensual, day-count 30/360). La calculadora del popup
        lo usa para sintetizar el cashflow virtual del bono."""
        if instrument is None or indices_provider is None:
            return None
        if not instrument.emission_date or not instrument.maturity_date:
            return None
        settle = ref_date if ref_date is not None else date.today()
        if instrument.maturity_date <= settle:
            return None
        return _tamar_dual_payoff_at(
            instrument, settle, indices_provider,
            tamar_forecast=tamar_forecast, to_date=instrument.maturity_date,
        )

    @staticmethod
    def recompute_as_tamar_puro(snapshot: MarketSnapshot, indices_provider):
        """Re-value a DUAL bond as if it were a pure TAMAR PURO bullet — strip the
        fixed floor / CER rail so only the TAMAR accrual contributes. Returns
        (tir, technical_value, modified_duration). Used in the TAMAR panel to
        show the same bond under the TAMAR-only scenario (suffixed `_TAM`)."""
        from dataclasses import replace

        inst = snapshot.instrument
        if inst is None:
            return None, None, None
        inst_clone = replace(
            inst,
            instrument_type="PURO",
            floor_rate_monthly=None,
        )
        snap_clone = replace(snapshot, instrument=inst_clone)
        tir = FinancialEngine.calculate_tir(snap_clone, indices_provider=indices_provider)
        vtec = FinancialEngine.calculate_technical_value(snap_clone, indices_provider=indices_provider)
        md = FinancialEngine.calculate_duration(snap_clone, tir) if tir is not None else None
        return tir, vtec, md

    @staticmethod
    def recompute_as_tasa_fija(snapshot: MarketSnapshot, indices_provider):
        """Re-value a DUAL bond as if only the fixed floor accrues — forces
        TAMAR to zero so max(TAMAR, floor) always resolves to floor.
        Returns (tir, technical_value, modified_duration). None if no floor."""
        inst = snapshot.instrument
        if inst is None or not inst.floor_rate_monthly:
            return None, None, None

        class _ZeroTamar:
            _cache_tamar = {}
            def get_tamar(self, d=None):
                return 0.0

        zero = _ZeroTamar()
        tir = FinancialEngine.calculate_tir(snapshot, indices_provider=zero)
        vtec = FinancialEngine.calculate_technical_value(snapshot, indices_provider=zero)
        md = FinancialEngine.calculate_duration(snapshot, tir) if tir is not None else None
        return tir, vtec, md

    # ------------------------------------------------------------------ #
    # Metrics extra para la calculadora del popup de detalle (tab 3).
    # Trabajan sobre el Instrument + ref_date, sin depender del snapshot live.
    # ------------------------------------------------------------------ #

    @staticmethod
    def _period_bounds(instrument, ref_date: date) -> Optional[tuple]:
        """(period_start, next_cf) del cupón corriente. None si no hay cupón
        futuro con interés > 0.

        Reglas (orden):
          1. Si hay flow pasado en Excel → tomar el último.
          2. Inferir prev = next_cf − (365/freq) días. Si la inferida es
             posterior a emission, usar la inferida — es el último cupón real.
          3. Si la inferida cae antes de emission, el bond aún no pagó su
             primer cupón: el período arranca en emission.

        Sin esto, bonos soberanos mid-amortización (AL29D, AL30D, GD30D...)
        cuyo Excel sólo trae flows futuros, calculaban accrued sobre todo
        el lapso emisión → próximo cupón (años, no meses) y daban V.Téc /
        intereses corridos completamente inflados.
        """
        if not instrument or not instrument.cashflows:
            return None
        cfs = sorted(instrument.cashflows, key=lambda c: c.date)
        past = [c for c in cfs if c.date < ref_date]
        future = [c for c in cfs if c.date >= ref_date]
        if not future or future[0].interest <= 0:
            return None
        next_cf = future[0]
        if past:
            return past[-1].date, next_cf
        freq = getattr(instrument, "payment_frequency", 2) or 2
        # relativedelta hace aritmética calendario: 12/freq meses → fecha exacta
        # del cupón anterior (Jul 9 − 6m = Jan 9, no Jan 8). days=365/freq daba
        # off-by-1 entre cada cupón porque año real = 365.25, no 365.
        months_between = max(12 // freq, 1)
        inferred_prev = next_cf.date - relativedelta(months=months_between)
        if instrument.emission_date and instrument.emission_date > inferred_prev:
            return instrument.emission_date, next_cf
        return inferred_prev, next_cf

    @staticmethod
    def accrued_interest(instrument, ref_date: date) -> float:
        """Intereses corridos per-100-VN, accrual lineal sobre el cupón corriente.
        0 para zero-coupon / capitalizables (LECER, LECAP, BONCER ZC, PURO, DUAL)."""
        bounds = FinancialEngine._period_bounds(instrument, ref_date)
        if not bounds:
            return 0.0
        period_start, next_cf = bounds
        period_days = (next_cf.date - period_start).days
        if period_days <= 0:
            return 0.0
        elapsed = (ref_date - period_start).days
        if elapsed <= 0:
            return 0.0
        elapsed = min(elapsed, period_days)
        return next_cf.interest * elapsed / period_days

    @staticmethod
    def days_since_last_coupon(instrument, ref_date: date) -> Optional[int]:
        """Días desde el último corte de cupón (o desde emisión si nunca pagó)."""
        bounds = FinancialEngine._period_bounds(instrument, ref_date)
        if not bounds:
            return None
        return max(0, (ref_date - bounds[0]).days)

    @staticmethod
    def residual_nominal(instrument, ref_date: date) -> float:
        """VR (Valor Residual) per-100. = suma de amortizaciones futuras.
        Fallback a 100 − amortizado para schedules incompletos."""
        if not instrument or not instrument.cashflows:
            return 100.0
        past = [c for c in instrument.cashflows if c.date < ref_date]
        future = [c for c in instrument.cashflows if c.date >= ref_date]
        residual = sum(c.amortization for c in future)
        if residual <= 0:
            amortized = sum(c.amortization for c in past)
            residual = max(100.0 - amortized, 0.0)
        return residual

    @staticmethod
    def current_yield(instrument, price_dirty: float, ref_date: date) -> Optional[float]:
        """Annual coupon / dirty price (decimal). Sum de intereses futuros
        en los próximos 12 meses / price."""
        if not instrument or not instrument.cashflows or not price_dirty or price_dirty <= 0:
            return None
        horizon = ref_date + timedelta(days=365)
        future_year_interest = sum(
            c.interest for c in instrument.cashflows
            if ref_date <= c.date <= horizon and c.interest > 0
        )
        if future_year_interest <= 0:
            return None
        return future_year_interest / price_dirty

    @staticmethod
    def _vanilla_pv(instrument, tir: float, ref_date: date) -> Optional[float]:
        """PV de los flujos futuros descontados al tir. None si no hay flujos."""
        if not instrument or tir is None or tir <= -1.0:
            return None
        future = [c for c in (instrument.cashflows or []) if c.date >= ref_date]
        if not future:
            return None
        pv = 0.0
        for cf in future:
            t = (cf.date - ref_date).days / _JULIAN_YEAR
            if t <= 0:
                continue
            pv += cf.total / (1.0 + tir) ** t
        return pv if pv > 0 else None

    @staticmethod
    def dv01(instrument, tir: float, ref_date: date) -> Optional[float]:
        """Cambio de precio per-100 ante -1bp en TIR. ΔP = P(tir-1bp) − P(tir).
        Sign positivo (precio sube cuando yield baja)."""
        if instrument is None or tir is None:
            return None
        p0 = FinancialEngine._vanilla_pv(instrument, tir, ref_date)
        p1 = FinancialEngine._vanilla_pv(instrument, tir - 0.0001, ref_date)
        if p0 is None or p1 is None:
            return None
        return p1 - p0

    @staticmethod
    def convexity(instrument, tir: float, ref_date: date) -> Optional[float]:
        """Convexidad en años². C = (1/P) × Σ cf × t × (t+1) / (1+r)^(t+2).
        Pareja con MD para aproximar ΔP/P ≈ -MD×Δy + 0.5×C×(Δy)²."""
        if instrument is None or tir is None or tir <= -1.0:
            return None
        future = [c for c in (instrument.cashflows or []) if c.date >= ref_date]
        if not future:
            return None
        pv = 0.0
        weighted = 0.0
        for cf in future:
            t = (cf.date - ref_date).days / _JULIAN_YEAR
            if t <= 0:
                continue
            df = (1.0 + tir) ** t
            pv += cf.total / df
            weighted += cf.total * t * (t + 1.0) / ((1.0 + tir) ** (t + 2.0))
        if pv <= 0:
            return None
        return weighted / pv

    @staticmethod
    def tir_from_price(
        snapshot: MarketSnapshot, price_override: float,
        indices_provider=None, fx_provider=None,
        settle_date: Optional[date] = None,
        tamar_forecast: Optional[float] = None,
    ) -> Optional[float]:
        """Inversa de calculate_tir: TIR para un precio (dirty) dado.
        Reusa toda la lógica per-type vía replace del snapshot."""
        from dataclasses import replace
        if snapshot is None or price_override is None or price_override <= 0:
            return None
        snap_override = replace(snapshot, price=float(price_override))
        return FinancialEngine.calculate_tir(snap_override, indices_provider, fx_provider,
                                             settle_date=settle_date,
                                             tamar_forecast=tamar_forecast)

    @staticmethod
    def price_from_tir(
        snapshot: MarketSnapshot, tir: float,
        indices_provider=None, fx_provider=None,
        settle_date: Optional[date] = None,
        tamar_forecast: Optional[float] = None,
    ) -> Optional[float]:
        """Precio (dirty, en la moneda en que cotiza el bono — pesos para todo lo
        que toma data912) implícito al TIR dado. Branches por tipo de bono.
        `tamar_forecast` (decimal) sobrescribe la TAMAR forward usada por las
        ramas TAMAR PURO / DUAL / DUAL_CER_TAMAR.
        """
        if snapshot is None or tir is None:
            return None
        inst = snapshot.instrument
        if inst is None:
            return None
        settle_date = _resolve_settle(inst.instrument_type, settle_date)

        # DOLAR LINKED: TIR en USD; precio_pesos = 100/(1+tir)^t × FX.
        if _is_dolar_linked_type(inst.instrument_type) and fx_provider:
            fx = fx_provider.get_mayorista_venta()
            if not (fx and fx > 0 and inst.maturity_date and inst.maturity_date > settle_date):
                return None
            years = (inst.maturity_date - settle_date).days / _JULIAN_YEAR
            return 100.0 / (1.0 + tir) ** years * fx

        # TAMAR PURO/DUAL/DUAL_CER_TAMAR: precio = payback / (1+tir)^years.
        # Payback con fórmula oficial BONTE (monthly capitalization).
        if (_is_tamar_puro_type(inst.instrument_type)
                or _is_dual_tamar_type(inst.instrument_type)
                or _is_dual_cer_tamar_type(inst.instrument_type)) \
                and indices_provider and inst.emission_date \
                and inst.maturity_date and inst.maturity_date > settle_date:
            payback = _tamar_dual_payoff_at(
                inst, settle_date, indices_provider,
                tamar_forecast=tamar_forecast, to_date=inst.maturity_date,
            )
            if payback is None:
                return None
            years = (inst.maturity_date - settle_date).days / _JULIAN_YEAR
            return payback / (1.0 + tir) ** years

        # CER: precio real → multiplicar por CER_LIQ/CER_BASE.
        if _is_cer_type(inst.instrument_type) and indices_provider and inst.cer_base:
            real_price = FinancialEngine._vanilla_pv(inst, tir, settle_date)
            if real_price is None:
                return None
            target_s = _cer_reference_date(settle_date, inst.cer_lag)
            cer_s = indices_provider.get_cer(target_s)
            if not cer_s:
                return real_price
            return real_price * (cer_s / inst.cer_base)

        # Vanilla (Soberanos, BOPREALES, LECAP, BONOFIJA).
        return FinancialEngine._vanilla_pv(inst, tir, settle_date)

    @staticmethod
    def tea_to_tem(tea: Optional[float]) -> Optional[float]:
        """TEA → TEM, act/365 convention: (1+TEA)^(30/365) − 1.

        Uses 30-day month over a 365-day year so the TEM is comparable across
        instruments regardless of their payment frequency. The AR market default.
        """
        if tea is None or tea <= -1.0:
            return None
        try:
            return (1.0 + tea) ** (30.0 / 365.0) - 1.0
        except (ValueError, OverflowError):
            return None

    @staticmethod
    def tea_to_tna_monthly(tea: Optional[float]) -> Optional[float]:
        """TEA → TNA m=12 (mensual capitalizable): 12 × ((1+TEA)^(1/12) − 1).

        Convención preferida para TAMAR/DUAL y otros bonos con capitalización
        mensual — el TNA base 365 (`tea_to_tna`) sub-representa la tasa
        efectiva del cupón mensual. Modelo Balanz/IAMC lo muestra como
        'Tasa Nominal' en bonos TAMAR.
        """
        if tea is None or tea <= -1.0:
            return None
        try:
            return 12.0 * ((1.0 + tea) ** (1.0 / 12.0) - 1.0)
        except (ValueError, OverflowError):
            return None

    @staticmethod
    def tea_to_tna(tea: Optional[float]) -> Optional[float]:
        """TEA → TNA, base 365: 365 × ((1+TEA)^(1/365) − 1).

        Daily-compounded nominal annual rate. Same 365-day basis as TEM, so the
        two are comparable across bonds.
        """
        if tea is None or tea <= -1.0:
            return None
        try:
            return 365.0 * ((1.0 + tea) ** (1.0 / 365.0) - 1.0)
        except (ValueError, OverflowError):
            return None

    @staticmethod
    def tea_to_tem_m12(tea: Optional[float]) -> Optional[float]:
        """TEA → TEM mensual m=12 (convencion 30/360 Secretaría de Finanzas):
        (1+TEA)^(1/12) − 1.

        Base oficial para LECAPs/BONCAPs según la fórmula VPV = VNO×(1+TEM)^t
        publicada por la Secretaría de Finanzas (base 30/360). Distinta de
        `tea_to_tem` que usa acto/365.
        """
        if tea is None or tea <= -1.0:
            return None
        try:
            return (1.0 + tea) ** (1.0 / 12.0) - 1.0
        except (ValueError, OverflowError):
            return None
