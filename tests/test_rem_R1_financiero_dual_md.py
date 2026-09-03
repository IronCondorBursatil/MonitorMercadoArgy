"""Auditoría R1 — Modified Duration de DUAL_CER_TAMAR: ¿de dónde salió el −31%?

CONTEXTO. Al unificar la TIR de `DualCerTamarStrategy` contra el payoff de
max-rieles (TEA **nominal**, igual que TAMAR PURO/DUAL) la MD de TXMJ8 pasó de
1.8048 a 1.2506 años. La auditoría preguntó si eso es consecuencia legítima de
la unificación o un síntoma de la regresión del lag CER.

VEREDICTO (lo fijan los tests de acá):

1. NO es el lag. En el escenario reportado manda el riel TAMAR, así que el
   `cer_lag` no toca ni el payoff ni la TIR ni la MD (`test_..._no_viene_del_lag`).
   El movimiento es 100% el cambio de UNIDAD de la TIR: antes era una tasa REAL
   ZC (0.88%), ahora es la TEA nominal del payoff (45.58%), y
   `MD = años/(1+tir)^(1/m)` es monótona decreciente en `tir`.
2. SÍ había, en cambio, una incoherencia de convención que la unificación dejó a
   la vista: `duration` usaba **m=1** mientras TAMAR PURO/DUAL —con los que ahora
   comparte la definición de TIR, el panel `dual_tamar` y el eje X de la curva
   'tamar'— usan **m=12**. `agents.md` › "Bonos TAMAR (PURO, DUAL,
   DUAL_CER_TAMAR)" dice "MD bullet TAMAR/DUAL usa m=12 (capitalización
   mensual)"; la excepción explícita m=1 es Dólar Linked, no este tipo. Se
   alineó a **m=12** (1.2506 → 1.7646 en el escenario reportado).
"""
from __future__ import annotations

from datetime import date

import pytest

from core.domain.conventions import cer_reference_date
from core.domain.models import Instrument, MarketSnapshot
from core.domain.pricing.tamar import tamar_dual_payoff_at
from core.domain.services import FinancialEngine

_EMISION = date(2026, 5, 15)
_VTO = date(2028, 6, 30)
_SETTLE = date(2026, 9, 4)
_CER_BASE = 758.117956
_PRECIO = 105.0


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setenv("MONITOR_AS_OF", "2026-09-03")


class _IndicesDelReporte:
    """El escenario exacto del hallazgo: CER +2%/mes, TAMAR 30% TNA → manda TAMAR."""

    def get_cer(self, d):
        return _CER_BASE * 1.02 ** ((d - _EMISION).days / 30.0)

    def get_tamar(self, d):
        return 30.0

    @property
    def _cache_tamar(self):
        return {date(2026, 9, 1): 30.0}


def _dual_cer_tamar(cer_lag: int = 10) -> Instrument:
    return Instrument(
        ticker="TXMJ8", short_name="Dual CER/TAMAR", instrument_type="DUAL_CER_TAMAR",
        emission_date=_EMISION, maturity_date=_VTO, cer_base=_CER_BASE,
        cer_spread=0.04, spread_rate=0.05, cer_lag=cer_lag, cashflows=[])


def _dual_tamar() -> Instrument:
    """Mismo bullet, tipo DUAL (TAMAR con floor) — el vecino de panel."""
    return Instrument(
        ticker="TTM26", short_name="Dual TAMAR", instrument_type="DUAL",
        emission_date=_EMISION, maturity_date=_VTO, spread_rate=0.05,
        floor_rate_monthly=0.0, cashflows=[])


def _tir_md(inst, idx):
    snap = MarketSnapshot(instrument=inst, price=_PRECIO)
    tir = FinancialEngine.calculate_tir(snap, idx, None, settle_date=_SETTLE)
    return tir, FinancialEngine.calculate_duration(snap, tir, settle_date=_SETTLE)


# ── 1 · el movimiento de MD NO es un síntoma de la regresión del lag CER ──────

def test_la_md_no_viene_del_lag_cer_en_el_escenario_reportado():
    """Con el riel TAMAR mandando, cambiar `cer_lag` no mueve payoff, TIR ni MD."""
    idx = _IndicesDelReporte()
    con_lag, sin_lag = _dual_cer_tamar(cer_lag=10), _dual_cer_tamar(cer_lag=0)
    assert (tamar_dual_payoff_at(con_lag, _SETTLE, idx, to_date=_VTO)
            == pytest.approx(tamar_dual_payoff_at(sin_lag, _SETTLE, idx, to_date=_VTO)))
    assert _tir_md(con_lag, idx) == pytest.approx(_tir_md(sin_lag, idx))


def _tir_real_zc_vieja(inst, idx) -> float:
    """La TIR que devolvía el motor PREVIO al fix: tasa REAL de un BONCER cero
    cupón — deflacta el precio por `CER(settle − cer_lag)/cer_base` y lo compara
    contra un redemption FIJO de 100 (sin `cer_spread`, sin riel TAMAR).

    Se RECOMPUTA desde el índice mock en vez de hardcodear 0.0087703: así el test
    sigue describiendo el mismo escenario si el mock cambia, y la comparación
    contra el motor de hoy es real y no un número pegado.
    """
    cer_s = idx.get_cer(cer_reference_date(_SETTLE, inst.cer_lag))
    real_price = _PRECIO / (cer_s / inst.cer_base)
    return (100.0 / real_price) ** (1.0 / inst.year_fraction_to(_VTO, _SETTLE)) - 1.0


def test_la_md_se_movio_porque_se_movio_la_tir():
    """La MD de TXMJ8 pasó de 1.8048 a 1.7646: ¿fue la TIR o fue el `m`?

    El test anterior era DECORATIVO: `años/(1+0.0087703)^1 ≈ 1.8048` es aritmética
    sobre dos constantes escritas a mano — no tocaba el motor y pasaba con
    cualquier fix revertido. Ahora las dos puntas salen del motor/el índice y se
    descompone el movimiento en sus dos factores.

    VEREDICTO: manda la TIR. A `m` constante, cambiar la unidad de la TIR (real ZC
    0.88% → TEA nominal 45.58%) mueve 0.554 años; a TIR constante, cambiar m=1→12
    mueve 0.015. Un factor ~38 entre uno y otro.
    """
    idx = _IndicesDelReporte()
    inst = _dual_cer_tamar()
    years = inst.year_fraction_to(_VTO, _SETTLE)
    tir_nom, md = _tir_md(inst, idx)                 # motor de HOY

    # Punta vieja (recomputada): TIR real ZC + m=1 → la MD reportada de 1.8048.
    tir_real = _tir_real_zc_vieja(inst, idx)
    md_vieja = years / (1.0 + tir_real) ** 1.0
    assert tir_real == pytest.approx(0.0087704, abs=1e-6)
    assert md_vieja == pytest.approx(1.8048, abs=1e-3)

    # Punta nueva: TEA nominal del payoff de max-rieles + m=12.
    assert tir_nom == pytest.approx(0.455811, abs=1e-5)
    assert tir_nom > 40 * tir_real, "la unidad de la TIR es otra, no un ajuste fino"
    assert md == pytest.approx(years / (1.0 + tir_nom) ** (1.0 / 12.0), rel=1e-12)

    # Descomposición: efecto de la TIR (a m=1) vs efecto del m (a TIR vieja).
    efecto_tir = abs(md_vieja - years / (1.0 + tir_nom) ** 1.0)
    efecto_m = abs(md_vieja - years / (1.0 + tir_real) ** (1.0 / 12.0))
    assert efecto_tir == pytest.approx(0.5542, abs=1e-3)
    assert efecto_m == pytest.approx(0.0145, abs=1e-3)
    assert efecto_tir > 25 * efecto_m, f"{efecto_tir=} {efecto_m=}"


# ── 2 · la convención m: alineada a la familia TAMAR (m=12) ───────────────────

def test_md_de_dual_cer_tamar_usa_m_12():
    """agents.md: "MD bullet TAMAR/DUAL usa m=12 (capitalización mensual)"."""
    idx = _IndicesDelReporte()
    inst = _dual_cer_tamar()
    tir, md = _tir_md(inst, idx)
    years = inst.year_fraction_to(_VTO, _SETTLE)
    assert md == pytest.approx(years / (1 + tir) ** (1.0 / 12.0), rel=1e-12)
    # y NO el m=1 que usaba antes (la diferencia es del 41%, no es ruido)
    assert md != pytest.approx(years / (1 + tir), rel=1e-3)


def test_md_comparte_convencion_con_su_vecino_de_panel_dual_tamar():
    """DUAL y DUAL_CER_TAMAR conviven en el panel `dual_tamar` y en el eje X de la
    curva 'tamar': con m distinto, las MD no serían comparables entre sí."""
    idx = _IndicesDelReporte()
    dct, dual = _dual_cer_tamar(), _dual_tamar()
    tir_dct, md_dct = _tir_md(dct, idx)
    tir_dual, md_dual = _tir_md(dual, idx)
    years = dct.year_fraction_to(_VTO, _SETTLE)
    assert dual.year_fraction_to(_VTO, _SETTLE) == pytest.approx(years)
    # misma fórmula, mismo m: la MD sólo difiere por la TIR de cada uno
    assert md_dct / md_dual == pytest.approx(
        ((1 + tir_dual) / (1 + tir_dct)) ** (1.0 / 12.0), rel=1e-12)


def test_ancla_numerica_de_la_md_del_escenario_reportado():
    """TXMJ8 @105, settle 2026-09-04, CER +2%/mes, TAMAR 30% TNA."""
    tir, md = _tir_md(_dual_cer_tamar(), _IndicesDelReporte())
    assert tir == pytest.approx(0.455811, abs=1e-5)
    assert md == pytest.approx(1.764572, abs=1e-5)
