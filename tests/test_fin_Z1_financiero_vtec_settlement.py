"""Lote Z1 — el V.Téc de DUAL_CER_TAMAR tiene DOS escalones, no uno.

REGRESIÓN RESIDUAL (la que este archivo cierra). El HEAD original hacía, en
`DualCerTamarStrategy.technical_value`:

    cer_reference_date(settlement_byma_date(ref, ctx.settle_lag), inst.cer_lag)

o sea PRIMERO la liquidación T+N y DESPUÉS el lag de 10 días hábiles de NT8/2024.
Al unificar el V.Téc contra `tamar.tamar_dual_payoff_at` se perdieron los dos; la
ronda anterior restauró el **lag** dentro del payoff pero NO el **escalón de
liquidación**, así que el V.Téc quedó indexando por el CER de la rueda en vez del
de su liquidación (y la paridad del panel, con él).

Estos tests fijan LOS DOS escalones a la vez: el escenario distingue las cuatro
combinaciones posibles con un CER escalonado, así que ninguna puede colarse.

Contrato completo (settlement T+N → lag CER → spread → max de rieles): docstring
de `core/domain/pricing/tamar.py`.
"""
from __future__ import annotations

from datetime import date

import pytest

from core.domain.conventions import cer_reference_date, settlement_byma_date
from core.domain.models import Instrument, MarketSnapshot
from core.domain.pricing.tamar import project_cer_at, tamar_dual_payoff_at
from core.domain.services import FinancialEngine
from core.domain.xirr import _JULIAN_YEAR

_EMISION = date(2026, 5, 15)
_VTO = date(2028, 6, 30)
_RUEDA = date(2026, 9, 4)      # viernes hábil: la fecha de referencia del V.Téc
_HOY = "2026-09-08"            # martes: deja las 4 fechas candidatas en el pasado
_CER_BASE = 100.0
_SPREAD_CER = 0.04
_PRECIO = 105.0

# Las 4 fechas que puede terminar leyendo el riel CER del V.Téc, según qué
# escalón se aplique. Sólo `_D` es la correcta.
_A_CRUDA = _RUEDA                                             # 2026-09-04 (ninguno)
_B_SOLO_LIQ = settlement_byma_date(_RUEDA, lag=1)             # 2026-09-07 (sólo T+1)
_C_SOLO_LAG = cer_reference_date(_RUEDA, 10)                  # 2026-08-21 (sólo lag)
_D_CORRECTA = cer_reference_date(_B_SOLO_LIQ, 10)             # 2026-08-24 (los dos)


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setenv("MONITOR_AS_OF", _HOY)


class _IdxCuatroEscalones:
    """CER con un salto en CADA una de las 4 fechas candidatas.

    100 → 200 (2026-08-24) → 400 (2026-09-04) → 800 (2026-09-07). Así el riel CER
    del V.Téc vale distinto según qué escalones se hayan aplicado, y la diferencia
    es un factor 2 como mínimo: imposible confundirla con ruido numérico.
    TAMAR = 0 para que el `max` de rieles no tape el efecto (riel TAMAR = 100).
    """

    def get_cer(self, d):
        if d >= _B_SOLO_LIQ:
            return 800.0
        if d >= _A_CRUDA:
            return 400.0
        if d >= _D_CORRECTA:
            return 200.0
        return 100.0

    def get_tamar(self, d):
        return 0.0

    @property
    def _cache_tamar(self):
        return {date(2026, 9, 1): 0.0}


class _IdxSuave:
    """CER creciente 10%/mes: el camino de proyección a futuro (payoff a vto)."""

    def get_cer(self, d):
        return _CER_BASE * 1.10 ** ((d - _EMISION).days / 30.0)

    def get_tamar(self, d):
        return 0.0

    @property
    def _cache_tamar(self):
        return {date(2026, 9, 1): 0.0}


def _inst(cer_lag: int = 10) -> Instrument:
    return Instrument(
        ticker="TXMJ8", short_name="Dual CER/TAMAR", instrument_type="DUAL_CER_TAMAR",
        emission_date=_EMISION, maturity_date=_VTO,
        cer_base=_CER_BASE, cer_spread=_SPREAD_CER, spread_rate=0.0,
        cer_lag=cer_lag, cashflows=[],
    )


def _riel_cer(inst, idx, ref_cer: date, end: date) -> float:
    """Riel CER a fecha `end` leyendo el índice en `ref_cer` (spread devengado)."""
    cer = project_cer_at(ref_cer, idx)
    years = (end - inst.emission_date).days / _JULIAN_YEAR
    return 100.0 * (cer / inst.cer_base) * (1.0 + (inst.cer_spread or 0.0)) ** years


def _vtec(inst, idx, *, settle_lag: int = 1) -> float:
    return FinancialEngine.calculate_technical_value(
        MarketSnapshot(instrument=inst, price=_PRECIO), idx, None,
        ref_date=_RUEDA, settle_lag=settle_lag)


# ── Sanity del escenario: las 4 fechas son distintas y están donde digo ───────

def test_el_escenario_distingue_las_cuatro_combinaciones():
    assert (_A_CRUDA, _B_SOLO_LIQ, _C_SOLO_LAG, _D_CORRECTA) == (
        date(2026, 9, 4), date(2026, 9, 7), date(2026, 8, 21), date(2026, 8, 24))
    idx = _IdxCuatroEscalones()
    valores = [idx.get_cer(d) for d in (_C_SOLO_LAG, _D_CORRECTA, _A_CRUDA, _B_SOLO_LIQ)]
    assert valores == [100.0, 200.0, 400.0, 800.0]


# ── El V.Téc: liquidación T+N Y DESPUÉS lag CER ───────────────────────────────

def test_el_vtec_liquida_primero_y_despues_aplica_el_lag_cer():
    """El único valor admisible es el del CER de `liquidación(rueda) − 10 hábiles`."""
    inst, idx = _inst(), _IdxCuatroEscalones()
    assert _vtec(inst, idx) == pytest.approx(
        _riel_cer(inst, idx, _D_CORRECTA, _RUEDA), rel=1e-12)


@pytest.mark.parametrize("etiqueta,ref_mala", [
    ("sin ningún escalón (CER de la rueda)", _A_CRUDA),
    ("sólo liquidación, sin lag CER", _B_SOLO_LIQ),
    ("sólo lag CER, sin liquidación ← la regresión residual", _C_SOLO_LAG),
])
def test_el_vtec_no_usa_ninguna_de_las_referencias_incompletas(etiqueta, ref_mala):
    inst, idx = _inst(), _IdxCuatroEscalones()
    malo = _riel_cer(inst, idx, ref_mala, _RUEDA)
    bueno = _vtec(inst, idx)
    assert bueno != pytest.approx(malo, rel=1e-3), etiqueta
    # el escenario separa de verdad: factor ≥ 2 contra cada alternativa
    assert max(bueno, malo) / min(bueno, malo) >= 2.0, etiqueta


def test_el_escalon_de_liquidacion_sale_del_settle_lag_no_de_una_constante():
    """`settle_lag=0` (CI, el que usa el popup y el panel T+0) → la rueda YA es la
    liquidación: el ref CER vuelve a ser `rueda − 10 hábiles`."""
    inst, idx = _inst(), _IdxCuatroEscalones()
    assert settlement_byma_date(_RUEDA, lag=0) == _RUEDA   # viernes hábil
    assert _vtec(inst, idx, settle_lag=0) == pytest.approx(
        _riel_cer(inst, idx, _C_SOLO_LAG, _RUEDA), rel=1e-12)
    # y T+1 vs T+0 dan valores distintos (el parámetro se está usando)
    assert _vtec(inst, idx) == pytest.approx(2.0 * _vtec(inst, idx, settle_lag=0), rel=1e-12)


def test_el_lag_cer_sigue_saliendo_del_instrumento():
    """`cer_lag=0` → sólo queda el escalón de liquidación (CER del 2026-09-07)."""
    idx = _IdxCuatroEscalones()
    inst0 = _inst(cer_lag=0)
    assert _vtec(inst0, idx) == pytest.approx(
        _riel_cer(inst0, idx, _B_SOLO_LIQ, _RUEDA), rel=1e-12)
    assert _vtec(inst0, idx) == pytest.approx(4.0 * _vtec(_inst(), idx), rel=1e-12)


# ── El payoff a vencimiento NO lleva escalón de liquidación ───────────────────

def test_el_payoff_a_vencimiento_no_liquida_la_fecha_de_pago():
    """`end` ya es la fecha de PAGO: un T+1 de más adelantaría la indexación. El
    payoff va con `cer_settle_lag=None` y sólo aplica el lag de 10 hábiles."""
    inst, idx = _inst(), _IdxSuave()
    payoff = tamar_dual_payoff_at(inst, _RUEDA, idx, to_date=_VTO)
    assert payoff == pytest.approx(
        _riel_cer(inst, idx, cer_reference_date(_VTO, inst.cer_lag), _VTO), rel=1e-12)
    con_escalon = tamar_dual_payoff_at(inst, _RUEDA, idx, to_date=_VTO, cer_settle_lag=1)
    assert con_escalon > payoff, "el escenario no distingue el escalón de más"
    assert payoff != pytest.approx(con_escalon, rel=1e-6)


def test_la_tir_usa_el_payoff_sin_escalon_de_liquidacion():
    """La TIR descuenta el payoff de vencimiento: `ctx.settle` ya es la liquidación
    resuelta por `resolve_settle`, así que acá no va otro T+N."""
    inst, idx = _inst(), _IdxSuave()
    snap = MarketSnapshot(instrument=inst, price=_PRECIO)
    tir = FinancialEngine.calculate_tir(snap, idx, None, settle_date=_RUEDA)
    payoff = tamar_dual_payoff_at(inst, _RUEDA, idx, to_date=_VTO)
    years = inst.year_fraction_to(_VTO, _RUEDA)
    assert tir == pytest.approx((payoff / _PRECIO) ** (1.0 / years) - 1.0, rel=1e-12)
