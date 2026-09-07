"""Tests del módulo central de day-counts (`core.domain.daycount`).

Fuente única de verdad de `year_fraction()` — la fracción de año usada para
DESCONTAR flujos (TIR/duration/PV). Cubre:
  - Identidades por convención (ACT/365, ACT/365.25, 30/360, ACT/ACT ISDA).
  - `parse_day_count` tolerante (alias, blanks, basura).
  - Bordes: bisiestos, Feb-29, fines de mes, regla del 31→30, same-day, reversa.

Estos tests fijan el contrato ANTES de implementar el módulo (TDD red→green).
"""

from __future__ import annotations

import calendar
from datetime import date

import pytest

from core.domain.daycount import DayCount, parse_day_count, year_fraction
from core.domain.cashflow_synth import days_30_360


# --------------------------------------------------------------------------- #
# parse_day_count — tabla de alias
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("ACT/365", DayCount.ACT_365),
    ("act/365", DayCount.ACT_365),
    ("  ACT/365  ", DayCount.ACT_365),
    ("ACTUAL/365", DayCount.ACT_365),
    ("Actual/365", DayCount.ACT_365),
    ("ACT/365.25", DayCount.ACT_365_25),
    ("act/365.25", DayCount.ACT_365_25),
    ("ACT/365,25", DayCount.ACT_365_25),     # coma decimal (locale)
    ("ACTUAL/365.25", DayCount.ACT_365_25),
    ("30/360", DayCount.THIRTY_360),
    ("30E/360", DayCount.THIRTY_360),
    ("30/360 US", DayCount.THIRTY_360),
    ("ACT/ACT", DayCount.ACT_ACT),
    ("act/act", DayCount.ACT_ACT),
    ("ACTUAL/ACTUAL", DayCount.ACT_ACT),
    ("ACT/ACT ISDA", DayCount.ACT_ACT),
])
def test_parse_known_aliases(raw, expected):
    assert parse_day_count(raw) is expected


@pytest.mark.parametrize("raw", ["", "   ", None, "garbage", "xyz", "???", "366"])
def test_parse_unknown_defaults_to_365_25(raw):
    # default = convención soberana (la que el motor venía usando)
    assert parse_day_count(raw) is DayCount.ACT_365_25


def test_parse_365_25_before_365():
    # "365.25" NO debe matchear como "365" (prefijo) — chequeo de orden.
    assert parse_day_count("ACT/365.25") is DayCount.ACT_365_25
    assert parse_day_count("ACT/365") is DayCount.ACT_365


def test_parse_returns_enum_member():
    val = parse_day_count("ACT/365")
    assert isinstance(val, DayCount)
    # str-enum: el .value es el string canónico
    assert val.value == "ACT/365"


# --------------------------------------------------------------------------- #
# year_fraction — identidades por convención
# --------------------------------------------------------------------------- #

def test_same_day_is_zero_all_conventions():
    d = date(2026, 6, 1)
    for conv in DayCount:
        assert year_fraction(d, d, conv) == 0.0


@pytest.mark.parametrize("days", [1, 10, 30, 90, 180, 365, 366, 730, 1000])
def test_act_365_is_days_over_365(days):
    from datetime import timedelta
    start = date(2026, 1, 1)
    end = start + timedelta(days=days)
    assert year_fraction(start, end, DayCount.ACT_365) == pytest.approx(days / 365.0)


@pytest.mark.parametrize("days", [1, 10, 30, 90, 180, 365, 366, 730, 1000])
def test_act_365_25_is_days_over_365_25(days):
    from datetime import timedelta
    start = date(2026, 1, 1)
    end = start + timedelta(days=days)
    assert year_fraction(start, end, DayCount.ACT_365_25) == pytest.approx(days / 365.25)


def test_act_365_vs_365_25_gap():
    # El gap exacto que causaba el bug de ~1bp en ONs.
    start, end = date(2026, 1, 1), date(2028, 1, 1)
    yf365 = year_fraction(start, end, DayCount.ACT_365)
    yf36525 = year_fraction(start, end, DayCount.ACT_365_25)
    assert yf365 > yf36525                          # 365 da más año-fracción
    assert yf36525 / yf365 == pytest.approx(365.0 / 365.25)


# --------------------------------------------------------------------------- #
# 30/360 — debe coincidir EXACTO con days_30_360/360 (garantía de equivalencia)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("start,end", [
    (date(2026, 1, 1), date(2026, 7, 1)),
    (date(2026, 1, 31), date(2026, 2, 28)),
    (date(2026, 1, 31), date(2026, 7, 31)),     # 31→30 ambos extremos
    (date(2026, 1, 30), date(2026, 7, 31)),     # regla d2==31 & d1>=30 → d2=30
    (date(2025, 12, 31), date(2026, 12, 31)),
    (date(2024, 2, 29), date(2025, 2, 28)),
    (date(2026, 6, 15), date(2031, 6, 15)),
    (date(2026, 3, 31), date(2026, 4, 30)),
])
def test_thirty_360_matches_days_30_360(start, end):
    assert year_fraction(start, end, DayCount.THIRTY_360) == pytest.approx(
        days_30_360(start, end) / 360.0
    )


def test_thirty_360_full_year_is_one():
    assert year_fraction(date(2026, 1, 1), date(2027, 1, 1), DayCount.THIRTY_360) == pytest.approx(1.0)


def test_thirty_360_half_year_is_half():
    assert year_fraction(date(2026, 1, 1), date(2026, 7, 1), DayCount.THIRTY_360) == pytest.approx(0.5)


# `test_thirty_360_matches_days_30_360` de arriba es TAUTOLÓGICO: `DayCount.THIRTY_360` se
# implementa llamando a `days_30_360` (core/domain/daycount.py), así que compara la función
# consigo misma y no fija la CONVENCIÓN. Estos valores están calculados A MANO desde la
# definición 30/360 (ISDA "30/360 Bond Basis") que implementa cashflow_synth.days_30_360:
#   d1 = min(d1, 30); si d2 == 31 y d1 >= 30 → d2 = 30; sin regla especial de febrero;
#   días = 360·Δaño + 30·Δmes + (d2 − d1).
# Es la convención de la Secretaría de Finanzas para LECAP/BONCAP (S29Y6: 359 días → payoff
# 132.0438) y la de las ONs 30/360 (Telecom Clase 24 en test_golden_referencia). Fase 5 de
# agents.md §0.8.
@pytest.mark.parametrize("start,end,dias", [
    (date(2026, 1, 1), date(2026, 7, 1), 180),      # medio año exacto
    (date(2026, 1, 31), date(2026, 2, 28), 28),     # d1 31→30; febrero corto NO se ajusta: 28−30+30
    (date(2026, 1, 30), date(2026, 2, 28), 28),     # d1 ya es 30: mismo resultado que el 31
    (date(2026, 2, 28), date(2026, 3, 31), 33),     # d1=28 (<30) → d2=31 se queda: 30+3
    (date(2026, 1, 31), date(2026, 7, 31), 180),    # 31→30 en los DOS extremos
    (date(2026, 1, 30), date(2026, 7, 31), 180),    # d1=30 → d2 31→30
    (date(2026, 1, 15), date(2026, 7, 31), 196),    # d1=15 (<30) → d2=31 se queda: 180+16
    (date(2025, 12, 31), date(2026, 12, 31), 360),  # año completo entre 31s
    (date(2024, 2, 29), date(2025, 2, 28), 359),    # bisiesto: d1=29, d2=28 → 360−1
    (date(2026, 3, 31), date(2026, 4, 30), 30),     # 31→30 y 30: un mes
    (date(2026, 6, 15), date(2031, 6, 15), 1800),   # 5 años
    (date(2025, 5, 30), date(2026, 5, 29), 359),    # S29Y6: el caso del payoff 132.0438
])
def test_days_30_360_valores_a_mano(start, end, dias):
    assert days_30_360(start, end) == dias
    assert year_fraction(start, end, DayCount.THIRTY_360) == pytest.approx(dias / 360.0)


# --------------------------------------------------------------------------- #
# ACT/ACT ISDA — años completos, bisiestos, cruces de año
# --------------------------------------------------------------------------- #

def test_act_act_full_leap_year_is_one():
    # 2024 bisiesto: 366/366 = 1.0
    assert year_fraction(date(2024, 1, 1), date(2025, 1, 1), DayCount.ACT_ACT) == pytest.approx(1.0)


def test_act_act_full_nonleap_year_is_one():
    assert year_fraction(date(2023, 1, 1), date(2024, 1, 1), DayCount.ACT_ACT) == pytest.approx(1.0)


def test_act_act_split_leap_nonleap():
    # Dic 2024 (leap) → Feb 2025 (non-leap). 31 días en 2024 + 31 en 2025.
    start, end = date(2024, 12, 1), date(2025, 2, 1)
    expected = 31 / 366.0 + 31 / 365.0
    assert year_fraction(start, end, DayCount.ACT_ACT) == pytest.approx(expected)


def test_act_act_multi_year_spanning_leap():
    # 2023-06-01 → 2026-06-01: cruza 2024 (leap). Suma por año tocado.
    start, end = date(2023, 6, 1), date(2026, 6, 1)
    total = 0.0
    y = start.year
    while y <= end.year:
        ys = max(start, date(y, 1, 1))
        ye = min(end, date(y + 1, 1, 1))
        if ye > ys:
            total += (ye - ys).days / (366.0 if calendar.isleap(y) else 365.0)
        y += 1
    assert year_fraction(start, end, DayCount.ACT_ACT) == pytest.approx(total)


def test_act_act_close_to_act_365_for_short_nonleap():
    # En un período corto dentro de un año no bisiesto, ACT/ACT == ACT/365.
    start, end = date(2026, 3, 1), date(2026, 6, 1)
    assert year_fraction(start, end, DayCount.ACT_ACT) == pytest.approx(
        year_fraction(start, end, DayCount.ACT_365)
    )


# --------------------------------------------------------------------------- #
# Reversa (end < start) — signo negativo para ACT; 30/360 ya viene firmado
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("conv", list(DayCount))
def test_reversed_is_negative(conv):
    start, end = date(2026, 1, 1), date(2027, 1, 1)
    fwd = year_fraction(start, end, conv)
    bwd = year_fraction(end, start, conv)
    assert bwd == pytest.approx(-fwd)


# --------------------------------------------------------------------------- #
# Bisiestos / Feb-29 / fines de mes — matriz de bordes
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("year", [2020, 2024, 2028, 2032])
def test_leap_year_feb_has_29(year):
    assert calendar.isleap(year)
    # year_fraction sobre todo febrero del bisiesto (ACT/365)
    yf = year_fraction(date(year, 2, 1), date(year, 3, 1), DayCount.ACT_365)
    assert yf == pytest.approx(29 / 365.0)


@pytest.mark.parametrize("year", [2021, 2022, 2023, 2025, 2026])
def test_nonleap_year_feb_has_28(year):
    assert not calendar.isleap(year)
    yf = year_fraction(date(year, 2, 1), date(year, 3, 1), DayCount.ACT_365)
    assert yf == pytest.approx(28 / 365.0)


@pytest.mark.parametrize("d_end", [28, 29, 30, 31])
def test_month_end_variations_act365(d_end):
    # distintos fines de mes desde el día 1 — ACT/365 cuenta días reales
    start = date(2026, 1, 1)
    # enero tiene 31 días; usamos un mes con el día pedido
    end = date(2026, 7, d_end) if d_end <= 31 else date(2026, 7, 31)
    expected = (end - start).days / 365.0
    assert year_fraction(start, end, DayCount.ACT_365) == pytest.approx(expected)


def test_feb29_start_act_act():
    # Empezar exactamente en Feb-29 de un bisiesto.
    start, end = date(2024, 2, 29), date(2025, 2, 28)
    # 2024 leap: del 29-Feb al 1-Ene-2025 = 306 días /366; +58 días en 2025 /365
    days_2024 = (date(2025, 1, 1) - start).days
    days_2025 = (end - date(2025, 1, 1)).days
    expected = days_2024 / 366.0 + days_2025 / 365.0
    assert year_fraction(start, end, DayCount.ACT_ACT) == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# Monotonicidad / sanidad
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("conv", list(DayCount))
def test_monotonic_in_end_date(conv):
    start = date(2026, 1, 1)
    prev = year_fraction(start, date(2026, 2, 1), conv)
    for m in range(3, 13):
        cur = year_fraction(start, date(2026, m, 1), conv)
        assert cur > prev
        prev = cur


@pytest.mark.parametrize("conv", list(DayCount))
def test_positive_for_forward_period(conv):
    assert year_fraction(date(2026, 1, 1), date(2030, 1, 1), conv) > 0


# --------------------------------------------------------------------------- #
# Integración con el modelo — day_count_enum / year_fraction_to (sin ciclo)
# --------------------------------------------------------------------------- #

def test_no_import_cycle_models_then_daycount():
    # NO usar importlib.reload: recrea el enum DayCount y rompe el `is` de otros
    # tests. La robustez al orden de import se valida por separado (subprocess).
    import core.domain.daycount as dc
    import core.domain.models as m
    assert hasattr(m.Instrument, "day_count_enum")
    assert hasattr(m.Instrument, "year_fraction_to")
    assert hasattr(dc, "year_fraction")


@pytest.mark.parametrize("dc_str,expected", [
    ("ACT/365", DayCount.ACT_365),
    ("ACT/365.25", DayCount.ACT_365_25),
    ("30/360", DayCount.THIRTY_360),
    ("ACT/ACT", DayCount.ACT_ACT),
    ("", DayCount.ACT_365_25),
])
def test_instrument_day_count_enum(dc_str, expected):
    from core.domain.models import Instrument
    inst = Instrument(ticker="X", short_name="X", instrument_type="BONAR", day_count=dc_str)
    assert inst.day_count_enum is expected


def test_bopreal_forces_thirty_360_enum():
    from core.domain.models import Instrument
    # BOPREAL sin day_count explícito → 30/360 (espeja is_30_360)
    inst = Instrument(ticker="BPY26", short_name="BPY26", instrument_type="BOPREAL", day_count="")
    assert inst.day_count_enum is DayCount.THIRTY_360
    assert inst.is_30_360


def test_instrument_year_fraction_to_matches_year_fraction():
    from core.domain.models import Instrument
    inst = Instrument(ticker="X", short_name="X", instrument_type="HARD DOLLAR", day_count="ACT/365")
    ref, target = date(2026, 6, 1), date(2028, 6, 1)
    assert inst.year_fraction_to(target, ref) == pytest.approx(
        year_fraction(ref, target, DayCount.ACT_365)
    )
