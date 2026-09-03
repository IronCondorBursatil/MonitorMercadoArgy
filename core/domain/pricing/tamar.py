"""Motor de payoff TAMAR (fórmula oficial BONTE TAMAR) y proyección de CER.

Extraído de `services.py` SIN cambios de fórmula. Validado contra calculadora
IAMC para TTJ26 (precio 158.20 → V.Téc 146.39, payback 164.32,
TIR_EA 39.06%).

Capitalización MENSUAL con day-count 30/360:
    TAMAR_TEM = ((1 + TNA/k)^k)^(1/12) − 1     con k = 365/32
    Pago a vto = VNO × (1 + TEM_max)^N_meses
    DUAL: TEM_max = max(TAMAR_TEM, fixed_TEM_mensual)
    DUAL_CER_TAMAR: max(rail TAMAR monthly, rail CER ratio × (1+spread)^years)

======================================================================
CONTRATO DEL V.TÉC / PAYOFF DE **DUAL_CER_TAMAR** — LEER ANTES DE TOCAR
======================================================================
Esta cadena ya se rompió DOS veces (se perdió el lag CER al unificar el V.Téc
contra el payoff; se perdió el escalón de liquidación al restaurar el lag). Son
CUATRO pasos, en este orden, y cada uno mueve la paridad del panel:

  1. LIQUIDACIÓN  T+N   `settlement_byma_date(end, lag=cer_settle_lag)`
     El V.Téc de un bono indexado se paga contra el CER de su **liquidación**, no
     el de la fecha de rueda: `calculate_technical_value` recibe `ref` = fecha de
     referencia (hoy) + `settle_lag` (1 = 24hs, 0 = CI) y el ref CER arranca en
     T+N. Es la MISMA convención que `pricing/base.py` aplica a todo bono CER.
     Sólo corre en el camino V.Téc (`to_date == ref_date`, `cer_settle_lag`
     provisto); el payoff a vencimiento ya está parado en la fecha de pago, así
     que va con `cer_settle_lag=None` (sin escalón).

  2. LAG CER 10 HÁB.    `cer_reference_date(<paso 1>, instrument.cer_lag)`
     NT8/2024 (`agents.md` › "Bonos CER"): el CER que indexa un pago de fecha D
     es el de `cer_lag` días hábiles BYMA ANTES de D. Corre en los DOS caminos
     (V.Téc y payoff proyectado). Sin él, el riel CER se sobrestima ~0,9% con CER
     a 2%/mes (14-15 días corridos de indexación de más).

  3. SPREAD             `× (1 + cer_spread)^years`
     `cer_spread` es el spread contractual del riel CER (sólo DUAL_CER_TAMAR,
     serie TXMJ*), devengado act/365.25 desde la emisión. Ignorarlo hacía que la
     TIR saliera idéntica con spread 0.00 y 0.04.

  4. MAX DE RIELES      `max(payoff_tamar, payoff_cer)`
     agents.md › "Bonos TAMAR": *"Payoff a vto = max(rail_TAMAR, CER_ratio ×
     (1+cer_spread)^years)"*. Vale también para el V.Téc devengado al settle.

Es decir: **settlement T+N → lag CER 10 hábiles → spread → max de rieles**.
Guardas: `tests/test_fin_Z1_financiero_vtec_settlement.py` (los dos escalones),
`tests/test_rem_R1_financiero_cer_lag.py` (el lag en los dos caminos) y
`tests/test_aud_B_financiero_dual_cer_tamar.py` (spread + max + round-trip).
El espejo del motor congelado vive en `tests/_legacy_engine.py`.
"""

from __future__ import annotations

import threading
from datetime import date, timedelta
from typing import Optional

from core.domain.clock import today as _domain_today
from core.domain.conventions import (
    cer_reference_date, days_30_360, settlement_byma_date, tamar_tem,
)
from core.domain.xirr import _JULIAN_YEAR


def project_cer_at(target_date: date, indices_provider) -> Optional[float]:
    """Extrapolación COMPUESTA del índice CER a una fecha futura: toma el
    crecimiento de los últimos 30 días y lo capitaliza `(1+g)^meses`. Coarse para
    duals TXMJ* (vencen 2-3 años), pero su TIR queda acotada por el rail TAMAR."""
    today = _domain_today()   # clock inyectable (F1): congelable vía MONITOR_AS_OF
    cer_today = indices_provider.get_cer(today)
    cer_30_ago = indices_provider.get_cer(today - timedelta(days=30))
    if not cer_today or not cer_30_ago or cer_30_ago <= 0:
        return cer_today
    if target_date <= today:
        return indices_provider.get_cer(target_date)
    monthly_growth = cer_today / cer_30_ago - 1.0
    months_ahead = (target_date - today).days / 30.0
    return cer_today * (1.0 + monthly_growth) ** months_ahead


# Cache del avg TAMAR (decimal) por (emission, end, forecast_tna). Invalidado al
# cambiar el día calendario — la serie BCRA TAMAR no se mueve intraday. Sin esto,
# cada ciclo de refresh (5s) gasta ~3500 dict-lookups recomputando el mismo avg.
_AVG_TAMAR_CACHE: dict = {}
_AVG_TAMAR_DAY: Optional[date] = None
_AVG_TAMAR_LOCK = threading.Lock()


def avg_tamar_tna(
    period_start: date, period_end: date, indices_provider,
    forecast_tna: Optional[float] = None,
) -> Optional[float]:
    """Promedio aritmético simple de TAMAR (TNA decimal) sobre [start, end].

    - días <= today: TAMAR observada de BCRA (con forward-fill de get_tamar).
    - días > today: `forecast_tna` (decimal); si None, última TAMAR observada.

    Cacheado por (start, end, forecast) con invalidación diaria.
    """
    global _AVG_TAMAR_DAY
    if period_end <= period_start:
        return None
    today = _domain_today()   # clock inyectable (F1): congelable vía MONITOR_AS_OF
    # La identidad del provider entra en la key: sin ella, ZeroTamar (stub que fuerza
    # TAMAR=0 para revaluar un DUAL como tasa fija) comparte cache con el provider real
    # y devuelve el promedio del real → la pata _TF del popup quedaba sin sentido.
    key = (period_start, period_end, forecast_tna, type(indices_provider).__name__)
    with _AVG_TAMAR_LOCK:
        if _AVG_TAMAR_DAY != today:
            _AVG_TAMAR_CACHE.clear()
            _AVG_TAMAR_DAY = today
        cached = _AVG_TAMAR_CACHE.get(key)
        if cached is not None or key in _AVG_TAMAR_CACHE:
            return cached

    # Past portion: sumar TAMAR observada día por día sobre [start, min(today, end)].
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


def tamar_dual_payoff_at(
    instrument, ref_date: date, indices_provider,
    *, tamar_forecast: Optional[float] = None, to_date: Optional[date] = None,
    cer_settle_lag: Optional[int] = None,
) -> Optional[float]:
    """Valor per-100 del bono TAMAR PURO/DUAL/DUAL_CER_TAMAR a fecha `to_date`
    (default = maturity). Si `to_date == ref_date`, devuelve V.Téc al settle.

    `cer_settle_lag` (sólo DUAL_CER_TAMAR): plazo BYMA T+N que se aplica a `end`
    ANTES del lag CER — paso 1 del contrato del docstring del módulo. `None`
    (default) = sin escalón, que es lo correcto para el payoff a vencimiento
    (`end` ya es la fecha de pago). El camino V.Téc pasa `ctx.settle_lag`
    (1 = 24hs, 0 = CI)."""
    if instrument is None or indices_provider is None:
        return None
    if not instrument.emission_date:
        return None
    end = to_date if to_date is not None else instrument.maturity_date
    if not end or end <= instrument.emission_date:
        return None

    spread = instrument.spread_rate or 0.0
    avg_t = avg_tamar_tna(instrument.emission_date, end, indices_provider,
                          forecast_tna=tamar_forecast)
    if avg_t is None:
        return None
    tem_tamar = tamar_tem(avg_t + spread)
    if tem_tamar is None:
        return None

    # DUAL: max contra floor mensual.
    if instrument.is_dual_tamar and instrument.floor_rate_monthly is not None:
        tem_max = max(tem_tamar, instrument.floor_rate_monthly)
    else:
        tem_max = tem_tamar

    n_months = days_30_360(instrument.emission_date, end) / 30.0
    try:
        payoff_tamar = 100.0 * (1.0 + tem_max) ** n_months
    except (ValueError, OverflowError):
        return None

    # DUAL_CER_TAMAR: comparar contra rail CER al vencimiento.
    if instrument.is_dual_cer_tamar and instrument.cer_base and instrument.cer_base > 0:
        cer_spread = instrument.cer_spread or 0.0
        # PASOS 1 y 2 del contrato (ver docstring del módulo), EN ESTE ORDEN:
        #   1. ESCALÓN DE LIQUIDACIÓN T+N — sólo en el camino V.Téc, donde `end`
        #      es la fecha de RUEDA y el bono se indexa por el CER de su
        #      liquidación (misma convención que `pricing/base.py` para todo CER).
        #      El payoff a vencimiento pasa `cer_settle_lag=None`: `end` ya es la
        #      fecha de pago y un T+1 de más adelantaría la indexación un día.
        #   2. LAG DE 10 DÍAS HÁBILES BYMA (NT8/2024, `agents.md` › "Bonos CER"):
        #      el CER que indexa un pago de fecha D es el de `cer_lag` hábiles
        #      ANTES. Vale para los DOS caminos. Sin el lag el riel CER se
        #      sobrestima ~0,9% con CER a 2%/mes (14-15 días corridos de más) y el
        #      error va derecho a la paridad cuando ese riel domina el `max`.
        cer_end = (end if cer_settle_lag is None
                   else settlement_byma_date(end, lag=cer_settle_lag))
        cer_at_end = project_cer_at(
            cer_reference_date(cer_end, instrument.cer_lag), indices_provider)
        if cer_at_end:
            years = (end - instrument.emission_date).days / _JULIAN_YEAR
            payoff_cer = 100.0 * (cer_at_end / instrument.cer_base) * (1.0 + cer_spread) ** years
            return max(payoff_tamar, payoff_cer)

    return payoff_tamar
