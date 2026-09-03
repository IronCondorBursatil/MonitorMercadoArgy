"""Auditoría lote B — DUAL CER/TAMAR (serie TXMJ*): TIR, V.Téc y round-trip.

Tres defectos encadenados en `DualCerTamarStrategy`:

1. `tir` calculaba la TIR REAL de un BONCER cero-cupón que redime 100 en términos
   base — ignoraba `inst.cer_spread` (el spread contractual del riel CER) Y el
   riel TAMAR. Verificado: la TIR salía idéntica con cer_spread=0.00 y 0.04.
2. `technical_value` devolvía `100 × CER/cer_base` sin devengar el spread y sin el
   max de rieles que agents.md documenta ("DUAL_CER_TAMAR (max rails)").
3. El round-trip `tir` → `price_from_tir` estaba roto por UNIDADES: `tir` devolvía
   una tasa REAL mientras `price_from_tir` descuenta el payoff NOMINAL. Con precio
   105 el round-trip devolvía 204.76 (~1,95×).

CONVENCIÓN ELEGIDA: **TIR nominal (TEA) contra el payoff de max-rieles**, la misma
de TAMAR PURO/DUAL. Ver el docstring de `DualCerTamarStrategy` para el porqué.
"""
from __future__ import annotations

from datetime import date

import pytest

from core.domain.conventions import cer_reference_date, settlement_byma_date
from core.domain.models import Instrument, MarketSnapshot
from core.domain.pricing.tamar import tamar_dual_payoff_at
from core.domain.services import FinancialEngine

_EMISION = date(2026, 5, 15)
_VTO = date(2028, 6, 30)
_CER_BASE = 758.117956
_SETTLE = date(2026, 9, 4)
_HOY = "2026-09-03"


class _IndicesCerManda:
    """CER +4%/mes desde `cer_base` al 2026-05-15 y TAMAR 10% TNA: a vencimiento
    el riel CER (≈275) le gana al riel TAMAR (≈137)."""
    _TNA = 10.0
    _G = 1.04

    def get_cer(self, d):
        return _CER_BASE * (self._G ** ((d - _EMISION).days / 30.0))

    def get_tamar(self, d):
        return self._TNA

    @property
    def _cache_tamar(self):
        return {date(2026, 9, 1): self._TNA}


class _IndicesTamarManda(_IndicesCerManda):
    """CER +1%/mes y TAMAR 40% TNA: manda el riel TAMAR (≈255 vs ≈129)."""
    _TNA = 40.0
    _G = 1.01


class _IndicesDelReporte(_IndicesCerManda):
    """El escenario exacto del hallazgo: CER +2%/mes, TAMAR 30% TNA."""
    _TNA = 30.0
    _G = 1.02


def _inst(cer_spread: float) -> Instrument:
    return Instrument(
        ticker="TXMJ8", short_name="Dual CER/TAMAR", instrument_type="DUAL_CER_TAMAR",
        emission_date=_EMISION, maturity_date=_VTO,
        cer_base=_CER_BASE, cer_spread=cer_spread, spread_rate=0.05, cashflows=[],
    )


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setenv("MONITOR_AS_OF", _HOY)


def _tir(inst, price, idx):
    return FinancialEngine.calculate_tir(MarketSnapshot(instrument=inst, price=price),
                                         idx, None, settle_date=_SETTLE)


def _vt(inst, idx):
    return FinancialEngine.calculate_technical_value(
        MarketSnapshot(instrument=inst, price=100.0), idx, None, ref_date=_SETTLE)


# ── #1 · el cer_spread contractual tiene que entrar en la TIR ─────────────────

def test_el_cer_spread_cambia_la_tir_cuando_manda_el_riel_cer():
    idx = _IndicesCerManda()
    sin = _tir(_inst(0.00), 105.0, idx)
    con = _tir(_inst(0.04), 105.0, idx)
    assert sin is not None and con is not None
    assert con > sin, "el spread del riel CER tiene que subir la TIR"
    assert (con - sin) > 0.02, f"el spread casi no movió la TIR: {sin=} {con=}"


def test_el_cer_spread_es_indiferente_cuando_manda_el_riel_tamar():
    """El payoff es `max(rieles)`: si TAMAR domina, el spread CER no cambia nada."""
    idx = _IndicesTamarManda()
    assert _tir(_inst(0.00), 105.0, idx) == pytest.approx(_tir(_inst(0.04), 105.0, idx))


# ── #3 · round-trip tir ↔ price_from_tir ──────────────────────────────────────

@pytest.mark.parametrize("idx_cls", [_IndicesCerManda, _IndicesTamarManda])
@pytest.mark.parametrize("spread", [0.0, 0.04])
@pytest.mark.parametrize("price", [80.0, 105.0, 140.0])
def test_round_trip_precio_tir_precio(idx_cls, spread, price):
    inst = _inst(spread)
    idx = idx_cls()
    snap = MarketSnapshot(instrument=inst, price=price)
    tir = FinancialEngine.calculate_tir(snap, idx, None, settle_date=_SETTLE)
    assert tir is not None
    back = FinancialEngine.price_from_tir(snap, tir, idx, None, settle_date=_SETTLE)
    assert back == pytest.approx(price, rel=1e-9)


def test_tir_es_la_tea_nominal_del_payoff_de_max_rieles():
    """Convención documentada: `(payoff / precio)^(1/años) − 1`, igual que
    TAMAR PURO/DUAL — NO una tasa real contra un redemption fijo de 100."""
    inst, idx, price = _inst(0.04), _IndicesCerManda(), 105.0
    payoff = tamar_dual_payoff_at(inst, _SETTLE, idx, to_date=_VTO)
    years = inst.year_fraction_to(_VTO, _SETTLE)
    assert _tir(inst, price, idx) == pytest.approx((payoff / price) ** (1.0 / years) - 1.0)


# ── #2 · V.Téc: max de rieles, con el spread devengado ────────────────────────

def test_technical_value_es_el_max_de_rieles_devengado_al_settle():
    """`cer_settle_lag=1`: el V.Téc indexa por el CER de la LIQUIDACIÓN de `_SETTLE`
    (paso 1 del contrato de `pricing/tamar.py`), no por el de la rueda."""
    inst, idx = _inst(0.04), _IndicesCerManda()
    assert _vt(inst, idx) == pytest.approx(
        tamar_dual_payoff_at(inst, _SETTLE, idx, to_date=_SETTLE, cer_settle_lag=1))
    # y NO es el payoff sin escalón de liquidación (el escenario los distingue).
    assert _vt(inst, idx) != pytest.approx(
        tamar_dual_payoff_at(inst, _SETTLE, idx, to_date=_SETTLE), rel=1e-6)


def test_technical_value_devenga_el_cer_spread():
    idx = _IndicesCerManda()
    sin, con = _vt(_inst(0.00), idx), _vt(_inst(0.04), idx)
    assert con > sin, "el V.Téc tiene que devengar el spread del riel CER"


def test_technical_value_nunca_por_debajo_del_riel_cer_puro():
    """max(rieles) ≥ riel CER puro (100 × CER_ref/base).

    `CER_ref` = CER a `liquidación(settle) − cer_lag` días hábiles: primero el
    escalón T+1 y después el lag de NT8/2024 — el contrato completo está en el
    docstring de `core/domain/pricing/tamar.py`.
    """
    ref_cer = cer_reference_date(settlement_byma_date(_SETTLE, lag=1), _inst(0.0).cer_lag)
    for idx in (_IndicesCerManda(), _IndicesTamarManda()):
        inst = _inst(0.04)
        riel_cer = 100.0 * idx.get_cer(ref_cer) / _CER_BASE
        assert _vt(inst, idx) >= riel_cer * 0.999
    # Y cuando el riel CER manda, NO usa el CER del día: el lag de 10 hábiles no
    # se puede colar de vuelta sin poner esto en rojo.
    idx = _IndicesCerManda()
    assert _vt(_inst(0.04), idx) < 100.0 * idx.get_cer(_SETTLE) / _CER_BASE


# ── Ancla numérica del escenario del reporte ──────────────────────────────────

def test_ancla_del_escenario_reportado():
    """TXMJ8 (emisión 15-May-26, vto 30-Jun-28, cer_base 758.117956, spread_rate 5%),
    settle 2026-09-04, precio 105, CER +2%/mes y TAMAR 30% → el riel TAMAR manda."""
    inst, idx, price = _inst(0.04), _IndicesDelReporte(), 105.0
    payoff = tamar_dual_payoff_at(inst, _SETTLE, idx, to_date=_VTO)
    assert payoff == pytest.approx(208.0415, abs=1e-3)
    assert _tir(inst, price, idx) == pytest.approx(0.455811, abs=1e-5)
    snap = MarketSnapshot(instrument=inst, price=price)
    assert FinancialEngine.price_from_tir(
        snap, _tir(inst, price, idx), idx, None, settle_date=_SETTLE
    ) == pytest.approx(105.0, rel=1e-9)
