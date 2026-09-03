"""Auditoría R1 — el riel CER de DUAL_CER_TAMAR tiene que aplicar el lag de
10 días hábiles BYMA (NT8/2024, `agents.md` › "Bonos CER (NT N°8/2024)").

REGRESIÓN QUE INTRODUJIMOS: al unificar `DualCerTamarStrategy.technical_value`
contra `tamar.tamar_dual_payoff_at` (fix del `cer_spread`), el V.Téc dejó de
pasar por `cer_reference_date(settle, cer_lag)` y empezó a leer el CER de la
fecha CRUDA. La rama vieja (borrada) sí aplicaba el lag; el payoff nunca lo
aplicó. Estos tests fijan la convención en `tamar_dual_payoff_at`, o sea para
los DOS caminos (V.Téc al settle y payoff al vencimiento).

El OTRO escalón del V.Téc — la liquidación T+N que corre ANTES del lag — se fija
en `tests/test_fin_Z1_financiero_vtec_settlement.py`. El contrato completo
(settlement → lag CER → spread → max de rieles) está en el docstring de
`core/domain/pricing/tamar.py`.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.domain.conventions import (
    cer_reference_date, days_30_360, settlement_byma_date, tamar_tem,
)
from core.domain.models import Instrument, MarketSnapshot
from core.domain.pricing.tamar import (
    avg_tamar_tna, project_cer_at, tamar_dual_payoff_at,
)
from core.domain.services import FinancialEngine
from core.domain.xirr import _JULIAN_YEAR

_EMISION = date(2026, 5, 15)
_VTO = date(2028, 6, 30)
_SETTLE = date(2026, 9, 4)
_HOY = "2026-09-03"
_CER_BASE = 50.0
_SPREAD_CER = 0.04


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setenv("MONITOR_AS_OF", _HOY)


class _IdxEscalon:
    """CER plano en 100 y **escalón ×10 el 2026-08-26**.

    El settle (2026-09-04) cae DESPUÉS del escalón; su fecha de referencia CER
    (10 días hábiles antes = 2026-08-21) cae ANTES. Así, sin el lag el riel CER
    se decuplica y con el lag queda en 100 — la diferencia es imposible de
    confundir con ruido numérico.  TAMAR = 0 para que el `max` de rieles no
    tape el efecto (el riel TAMAR queda clavado en 100).
    """
    _SALTO = date(2026, 8, 26)

    def get_cer(self, d):
        return 1000.0 if d >= self._SALTO else 100.0

    def get_tamar(self, d):
        return 0.0

    @property
    def _cache_tamar(self):
        return {date(2026, 9, 1): 0.0}


class _IdxSuave:
    """CER creciente al 10% mensual (compuesto) — el camino de proyección a
    futuro de `project_cer_at`, que es el que usa el payoff a vencimiento."""

    def get_cer(self, d):
        return 100.0 * 1.10 ** ((d - _EMISION).days / 30.0)

    def get_tamar(self, d):
        return 0.0

    @property
    def _cache_tamar(self):
        return {date(2026, 9, 1): 0.0}


def _inst(cer_spread: float = _SPREAD_CER, cer_lag: int = 10) -> Instrument:
    return Instrument(
        ticker="TXMJ8", short_name="Dual CER/TAMAR", instrument_type="DUAL_CER_TAMAR",
        emission_date=_EMISION, maturity_date=_VTO,
        cer_base=_CER_BASE, cer_spread=cer_spread, spread_rate=0.0,
        cer_lag=cer_lag, cashflows=[],
    )


def _riel_cer(inst, idx, end: date, *, lag: bool, settle_lag: int | None = None) -> float:
    """El riel CER esperado a fecha `end`.

    `settle_lag` no-None reproduce el ESCALÓN DE LIQUIDACIÓN del camino V.Téc
    (paso 1 del contrato de `pricing/tamar.py`); `lag` reproduce el paso 2 (los
    `cer_lag` días hábiles de NT8/2024). Los dos escalones se fijan en
    `tests/test_fin_Z1_financiero_vtec_settlement.py`.
    """
    ref = end if settle_lag is None else settlement_byma_date(end, lag=settle_lag)
    if lag:
        ref = cer_reference_date(ref, inst.cer_lag)
    cer = project_cer_at(ref, idx)
    years = (end - inst.emission_date).days / _JULIAN_YEAR
    return 100.0 * (cer / inst.cer_base) * (1.0 + (inst.cer_spread or 0.0)) ** years


# ── V.Téc al settle (el camino que PERDIÓ el lag) ─────────────────────────────

def test_vtec_de_dual_cer_tamar_lee_el_cer_con_lag_de_10_habiles():
    inst, idx = _inst(), _IdxEscalon()
    vt = FinancialEngine.calculate_technical_value(
        MarketSnapshot(instrument=inst, price=100.0), idx, None, ref_date=_SETTLE)
    assert vt == pytest.approx(_riel_cer(inst, idx, _SETTLE, lag=True, settle_lag=1))


def test_vtec_de_dual_cer_tamar_no_usa_el_cer_del_dia():
    """Guarda anti-regresión: sin el lag el V.Téc se va ~10× (escalón del CER)."""
    inst, idx = _inst(), _IdxEscalon()
    vt = FinancialEngine.calculate_technical_value(
        MarketSnapshot(instrument=inst, price=100.0), idx, None, ref_date=_SETTLE)
    sin_lag = _riel_cer(inst, idx, _SETTLE, lag=False, settle_lag=1)
    assert sin_lag > vt * 5, "el escenario no distingue lag de no-lag"
    assert vt < sin_lag * 0.5


def test_el_lag_del_vtec_respeta_el_cer_lag_declarado_del_bono():
    """`cer_lag=0` (bono sin lag declarado) tiene que leer el CER de la
    LIQUIDACIÓN del settle: el lag sale del instrumento, no de una constante
    hardcodeada (el escalón T+1 sí es del contrato y se queda)."""
    idx = _IdxEscalon()
    inst0 = _inst(cer_lag=0)
    vt0 = FinancialEngine.calculate_technical_value(
        MarketSnapshot(instrument=inst0, price=100.0), idx, None, ref_date=_SETTLE)
    assert vt0 == pytest.approx(_riel_cer(inst0, idx, _SETTLE, lag=True, settle_lag=1))
    assert vt0 > 5 * FinancialEngine.calculate_technical_value(
        MarketSnapshot(instrument=_inst(), price=100.0), idx, None, ref_date=_SETTLE)


# ── Payoff a vencimiento (el camino que NUNCA aplicó el lag) ──────────────────

def test_el_payoff_a_vencimiento_proyecta_el_cer_a_la_fecha_de_referencia():
    inst, idx = _inst(), _IdxSuave()
    payoff = tamar_dual_payoff_at(inst, _SETTLE, idx, to_date=_VTO)
    assert payoff == pytest.approx(_riel_cer(inst, idx, _VTO, lag=True))


def test_el_payoff_a_vencimiento_no_proyecta_el_cer_al_dia_del_pago():
    """Sin el lag el payoff queda ~5% alto (10%/mes × 15 días corridos)."""
    inst, idx = _inst(), _IdxSuave()
    con_lag = _riel_cer(inst, idx, _VTO, lag=True)
    sin_lag = _riel_cer(inst, idx, _VTO, lag=False)
    # El escenario distingue de verdad (aritmética pura del helper, sin motor).
    assert sin_lag > con_lag * 1.03
    payoff = tamar_dual_payoff_at(inst, _SETTLE, idx, to_date=_VTO)
    assert payoff == pytest.approx(sin_lag * 1.10 ** (-15 / 30.0), rel=1e-9)
    assert payoff < sin_lag * 0.99


def test_la_fecha_de_referencia_cae_10_habiles_antes():
    """Sanity del escenario: 2028-06-30 − 10 hábiles = 2028-06-15 (15 corridos);
    2026-09-04 − 10 hábiles = 2026-08-21 (14 corridos, sin feriados en el medio)."""
    assert cer_reference_date(_VTO, 10) == date(2028, 6, 15)
    assert cer_reference_date(_SETTLE, 10) == date(2026, 8, 21)
    assert (_SETTLE - cer_reference_date(_SETTLE, 10)) == timedelta(days=14)


# ── El lag NO se aplica donde no corresponde ──────────────────────────────────

class _IdxTamar(_IdxEscalon):
    """El escalón de CER de `_IdxEscalon` + TAMAR 30% TNA (riel TAMAR vivo)."""

    def get_tamar(self, d):
        return 30.0

    @property
    def _cache_tamar(self):
        return {date(2026, 9, 1): 30.0}


def _puro(cer_lag: int = 10) -> Instrument:
    """TAMAR PURO con los campos CER **cargados igual que el dual**: si el riel
    CER se colara en un PURO, este bono lo delataría."""
    return Instrument(
        ticker="TTJ26", short_name="TAMAR puro", instrument_type="PURO",
        emission_date=_EMISION, maturity_date=_VTO, spread_rate=0.05,
        cer_base=_CER_BASE, cer_spread=_SPREAD_CER, cer_lag=cer_lag, cashflows=[],
    )


def _riel_tamar(inst, idx, end: date) -> float:
    """Fórmula oficial BONTE TAMAR calculada aparte (sin pasar por el payoff)."""
    tem = tamar_tem(avg_tamar_tna(inst.emission_date, end, idx) + (inst.spread_rate or 0.0))
    return 100.0 * (1.0 + tem) ** (days_30_360(inst.emission_date, end) / 30.0)


def test_el_riel_tamar_no_toca_el_lag_cer():
    """TAMAR PURO/DUAL comparten `tamar_dual_payoff_at` pero NO tienen riel CER:
    ni el `cer_lag` ni el escalón de liquidación pueden mover su payoff.

    El test anterior era DECORATIVO — comparaba la misma llamada contra sí misma
    (`f(x) == approx(f(x))`, línea 168), así que pasaba también con el lag
    revertido. Ahora: (a) el payoff PURO se contrasta contra la fórmula BONTE
    TAMAR computada aparte, (b) se lo prueba insensible al `cer_lag` declarado y
    al `cer_settle_lag`, y (c) un DUAL_CER_TAMAR con los MISMOS campos CER SÍ se
    mueve con el lag en el mismo escenario — el control que hace que (a) y (b) no
    sean vacíos.
    """
    idx = _IdxTamar()

    # (a) payoff a vto = fórmula BONTE TAMAR pura, sin rastro del índice CER.
    esperado_vto = _riel_tamar(_puro(), idx, _VTO)
    assert tamar_dual_payoff_at(_puro(), _SETTLE, idx, to_date=_VTO) == pytest.approx(
        esperado_vto, rel=1e-12)
    assert esperado_vto > 100.0, "el escenario tiene que devengar TAMAR de verdad"

    # (b) insensible al cer_lag declarado y al escalón de liquidación (V.Téc).
    assert tamar_dual_payoff_at(_puro(cer_lag=0), _SETTLE, idx, to_date=_VTO) == pytest.approx(
        esperado_vto, rel=1e-12)
    vt_puro = tamar_dual_payoff_at(_puro(), _SETTLE, idx, to_date=_SETTLE, cer_settle_lag=1)
    assert vt_puro == pytest.approx(_riel_tamar(_puro(), idx, _SETTLE), rel=1e-12)
    assert vt_puro == pytest.approx(
        tamar_dual_payoff_at(_puro(), _SETTLE, idx, to_date=_SETTLE), rel=1e-12)
    assert vt_puro > 100.0

    # (c) CONTROL — el MISMO escenario mueve al DUAL_CER_TAMAR: con lag lee el CER
    #     pre-escalón (100) y sin lag el post-escalón (~1000). Si esto empatara,
    #     los asserts de arriba no probarían nada.
    con_lag = tamar_dual_payoff_at(_inst(cer_lag=10), _SETTLE, idx,
                                   to_date=_SETTLE, cer_settle_lag=1)
    sin_lag = tamar_dual_payoff_at(_inst(cer_lag=0), _SETTLE, idx,
                                   to_date=_SETTLE, cer_settle_lag=1)
    assert sin_lag > 5 * con_lag, "el escenario no discrimina lag de no-lag"
