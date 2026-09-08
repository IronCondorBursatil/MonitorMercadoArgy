"""DUAL dólar-linked/TAMAR (`DUAL_DL_TAMAR`, caso TMVE8).

Spec: docs/superpowers/specs/2026-09-08-dual-dl-tamar-design.md. Payoff a vencimiento =
max(riel TAMAR capitalizado mensual desde la emisión, 100 × FX / TC inicial); TEA nominal en
pesos; el dólar es el del settle, sin proyectar.
"""
from __future__ import annotations

from datetime import date

import pytest

from core.domain.instrument_groups import BOND_TYPES, DUAL_DL, DUAL_TAMAR, is_known_type
from core.domain.models import Instrument

_EMISION = date(2026, 7, 31)
_VTO = date(2028, 1, 31)
_FX_BASE = 1300.0


def _inst(**kw) -> Instrument:
    base = dict(ticker="TMVE8", short_name="Dual DL/TAMAR", instrument_type="DUAL_DL_TAMAR",
                emission_date=_EMISION, maturity_date=_VTO, day_count="30/360",
                fx_base=_FX_BASE, spread_rate=0.0, cashflows=())
    base.update(kw)
    return Instrument(**base)


# ── Task 1: tipo, grupo y campo ─────────────────────────────────────────────
def test_el_tipo_existe_en_su_grupo_propio_y_no_en_dual_tamar():
    """Grupo propio a propósito: `apps/cli/bei.py` y la curva `tamar` toman DUAL_TAMAR como
    universo de TEA en pesos pura; un riel dólar la distorsionaría (spec §2)."""
    assert DUAL_DL == ["DUAL_DL_TAMAR"]
    assert "DUAL_DL_TAMAR" in BOND_TYPES and is_known_type(" dual_dl_tamar ")
    assert "DUAL_DL_TAMAR" not in DUAL_TAMAR


def test_predicados_del_modelo():
    i = _inst()
    assert i.is_dual_dl_tamar
    assert not i.is_cer and not i.is_dolar_linked and not i.is_hard_dollar
    assert not i.is_dual_cer_tamar and not i.is_dual_tamar and not i.is_tamar_puro
    assert not _inst(instrument_type="DUAL_CER_TAMAR").is_dual_dl_tamar
    assert i.fx_base == _FX_BASE
    assert Instrument(ticker="X", short_name="X", instrument_type="BONAR").fx_base is None


def test_fx_base_sale_de_tc_inicial_en_las_dos_puertas_de_lectura():
    """Motor (`_orm_to_domain`) y form/preview (`build_instrument`) leen el mismo campo de la
    hoja Dólar Linked; coma decimal tolerada; 0 o vacío = dato ausente."""
    from core.infrastructure.db.catalog_repository import _orm_to_domain
    from core.infrastructure.db.models import InstrumentORM
    from core.infrastructure.repositories import build_instrument

    orm = InstrumentORM(ticker="TMVE8", short_name="TMVE8", instrument_type="DUAL_DL_TAMAR",
                        day_count="30/360", cer_lag=10, payment_frequency=2,
                        raw_fields={"tc_inicial": "1300,5"})
    assert _orm_to_domain(orm).fx_base == 1300.5
    orm.raw_fields = {"tc_inicial": "0"}
    assert _orm_to_domain(orm).fx_base is None
    orm.raw_fields = {"tc_inicial": ""}
    assert _orm_to_domain(orm).fx_base is None
    orm.raw_fields = None
    assert _orm_to_domain(orm).fx_base is None

    row = {"ticker": "TMVE8", "tipo": "DUAL_DL_TAMAR", "fecha_emision": "2026-07-31",
           "fecha_vencimiento": "2028-01-31", "base calculo": "30/360", "tc_inicial": 1300.5}
    inst = build_instrument(row, "TAMAR", [])
    assert inst is not None and inst.instrument_type == "DUAL_DL_TAMAR"
    assert inst.fx_base == 1300.5 and inst.day_count == "30/360"
    assert build_instrument({**row, "tc_inicial": ""}, "TAMAR", []).fx_base is None
    assert build_instrument({**row, "tc_inicial": 0}, "TAMAR", []).fx_base is None


# ── Task 2: strategy ────────────────────────────────────────────────────────
from core.domain.conventions import days_30_360, tamar_tem  # noqa: E402
from core.domain.models import MarketSnapshot  # noqa: E402
from core.domain.pricing.context import PricingContext  # noqa: E402
from core.domain.pricing.registry import strategy_for  # noqa: E402
from core.domain.pricing.strategies import (  # noqa: E402
    DolarLinkedStrategy, DualCerTamarStrategy, DualDlTamarStrategy, TamarStrategy,
    dual_dl_tamar_payoff_at,
)
from core.domain.pricing.tamar import tamar_dual_payoff_at  # noqa: E402
from core.domain.services import FinancialEngine  # noqa: E402

_SETTLE = date(2026, 9, 8)
_HOY = "2026-09-07"
_TNA = 30.0            # TAMAR constante (TNA %)


class _Idx:
    """TAMAR constante; sin CER (este tipo no lo pide) y SIN get_a3500."""
    def __init__(self, tna=_TNA):
        self._tna = tna

    def get_tamar(self, d=None):
        return self._tna

    @property
    def _cache_tamar(self):
        return {date(2026, 9, 1): self._tna}

    def get_cer(self, d):
        return None


class _IdxConA3500(_Idx):
    def get_a3500(self, d=None):
        return 1500.0


class _Fx:
    def __init__(self, v):
        self._v = v

    def get_mayorista_venta(self):
        return self._v


class _FxCaido:
    def get_mayorista_venta(self):
        return None


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setenv("MONITOR_AS_OF", _HOY)


def _snap(inst, price):
    return MarketSnapshot(instrument=inst, price=price)


def _riel_tamar_a_mano():
    """100 × (1 + TEM)^N con N = días 30/360 / 30 — la fórmula BONTE TAMAR, sin pasar por
    tamar.py, para que el test no dependa de lo que pinea."""
    return 100.0 * (1.0 + tamar_tem(_TNA / 100.0)) ** (days_30_360(_EMISION, _VTO) / 30.0)


def test_registry_rutea_el_tipo_nuevo_sin_mover_a_los_vecinos():
    assert isinstance(strategy_for(_inst()), DualDlTamarStrategy)
    assert isinstance(strategy_for(_inst(instrument_type="PURO")), TamarStrategy)
    assert isinstance(strategy_for(_inst(instrument_type="DUAL")), TamarStrategy)
    assert isinstance(strategy_for(_inst(instrument_type="DUAL_CER_TAMAR", cer_base=700.0)),
                      DualCerTamarStrategy)
    assert isinstance(strategy_for(_inst(instrument_type="DOLAR_LINKED")), DolarLinkedStrategy)


def test_gana_el_riel_tamar_cuando_el_dolar_no_alcanza():
    """FX 1500 / 1300 = 115,4 < riel TAMAR (≈156): paga el riel TAMAR, exactamente el de
    la fórmula BONTE."""
    ctx = PricingContext(settle=_SETTLE, indices=_Idx(), fx=_Fx(1500.0))
    payoff = dual_dl_tamar_payoff_at(_inst(), _SETTLE, ctx)
    assert payoff == pytest.approx(_riel_tamar_a_mano(), rel=1e-12)
    assert payoff == pytest.approx(tamar_dual_payoff_at(_inst(), _SETTLE, _Idx(), to_date=_VTO))
    assert payoff > 100.0 * 1500.0 / _FX_BASE


def test_gana_el_riel_dl_cuando_el_dolar_supera_la_capitalizacion():
    """FX 2600 / 1300 = 200 > riel TAMAR (≈156): paga el riel DL, sin proyectar el dólar."""
    ctx = PricingContext(settle=_SETTLE, indices=_Idx(), fx=_Fx(2600.0))
    assert dual_dl_tamar_payoff_at(_inst(), _SETTLE, ctx) == pytest.approx(200.0)


def test_vtec_es_el_max_de_rieles_devengado_al_settle():
    """Al settle el riel TAMAR lleva ~1,3 meses (≈103) y el DL vale 115,4: manda el DL.
    Antes de la emisión, 100."""
    fx = _Fx(1500.0)
    vt = FinancialEngine.calculate_technical_value(_snap(_inst(), 100.0), _Idx(), fx,
                                                   ref_date=_SETTLE)
    tamar_al_settle = tamar_dual_payoff_at(_inst(), _SETTLE, _Idx(), to_date=_SETTLE)
    assert tamar_al_settle is not None and 100.0 < tamar_al_settle < 110.0
    assert vt == pytest.approx(max(tamar_al_settle, 100.0 * 1500.0 / _FX_BASE))
    assert vt == pytest.approx(100.0 * 1500.0 / _FX_BASE)
    pre = FinancialEngine.calculate_technical_value(_snap(_inst(), 100.0), _Idx(), fx,
                                                    ref_date=date(2026, 7, 1))
    assert pre == 100.0


def test_tir_es_tea_nominal_contra_el_payoff_y_md_usa_m12():
    inst = _inst()
    fx = _Fx(1500.0)
    tir = FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), fx, settle_date=_SETTLE)
    years = inst.year_fraction_to(_VTO, _SETTLE)
    assert tir == pytest.approx((_riel_tamar_a_mano() / 120.0) ** (1.0 / years) - 1.0, rel=1e-9)
    md = FinancialEngine.calculate_duration(_snap(inst, 120.0), tir, settle_date=_SETTLE)
    assert md == pytest.approx(years / (1.0 + tir) ** (1.0 / 12.0), rel=1e-9)


@pytest.mark.parametrize("fx_val", [1500.0, 2600.0])
@pytest.mark.parametrize("price", [90.0, 120.0, 150.0])
def test_round_trip_precio_tir_precio(fx_val, price):
    inst = _inst()
    fx = _Fx(fx_val)
    tir = FinancialEngine.calculate_tir(_snap(inst, price), _Idx(), fx, settle_date=_SETTLE)
    assert tir is not None
    back = FinancialEngine.price_from_tir(_snap(inst, price), tir, _Idx(), fx,
                                          settle_date=_SETTLE)
    assert back == pytest.approx(price, rel=1e-9)


def test_sin_tc_inicial_no_se_inventa_ni_se_precia_con_el_riel_tamar_solo():
    inst = _inst(fx_base=None)
    fx = _Fx(1500.0)
    assert FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), fx, settle_date=_SETTLE) is None
    assert FinancialEngine.calculate_technical_value(_snap(inst, 120.0), _Idx(), fx,
                                                     ref_date=_SETTLE) is None
    assert FinancialEngine.price_from_tir(_snap(inst, 120.0), 0.3, _Idx(), fx,
                                          settle_date=_SETTLE) is None


def test_sin_dolar_vivo_cae_al_a3500_del_bcra_y_sin_ninguno_devuelve_none():
    inst = _inst()
    con_a3500 = FinancialEngine.calculate_tir(_snap(inst, 120.0), _IdxConA3500(), _FxCaido(),
                                              settle_date=_SETTLE)
    vivo = FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), _Fx(1500.0),
                                         settle_date=_SETTLE)
    assert con_a3500 == pytest.approx(vivo)             # mismo dólar (1500) por otra vía
    assert FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), _FxCaido(),
                                         settle_date=_SETTLE) is None
    assert FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), None,
                                         settle_date=_SETTLE) is None


def test_el_forecast_tamar_mueve_solo_el_riel_tamar():
    inst = _inst()
    manda_tamar = _Fx(1500.0)
    base = FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), manda_tamar,
                                         settle_date=_SETTLE)
    alto = FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), manda_tamar,
                                         settle_date=_SETTLE, tamar_forecast=0.60)
    assert alto > base
    # FX bien arriba de 2600: a forecast=0.60 el riel TAMAR sube a ~233 (blend pasado/futuro
    # de avg_tamar_tna sobre 18 meses) — con 2600 (riel DL 200) el riel TAMAR forecast lo
    # superaría y el test dejaría de aislar el riel DL. 4000 → riel DL ~307,7, con margen.
    manda_dl = _Fx(4000.0)
    assert (FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), manda_dl,
                                          settle_date=_SETTLE, tamar_forecast=0.60)
            == pytest.approx(FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(),
                                                           manda_dl, settle_date=_SETTLE)))


def test_vencido_cae_al_camino_general():
    inst = _inst(maturity_date=date(2026, 6, 30))
    assert FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), _Fx(1500.0),
                                         settle_date=_SETTLE) is None   # sin flujos: vanilla → None


def test_el_tipo_es_analitico():
    from core.domain.instrument_groups import ANALYTIC_PAYOFF_TYPES, has_closed_form_payoff
    assert "DUAL_DL_TAMAR" in ANALYTIC_PAYOFF_TYPES and has_closed_form_payoff("dual_dl_tamar")


def test_el_tipo_entra_al_panel_tamar_al_pricing_y_a_la_cartera_pero_no_a_bei_ni_curva():
    from apps.cli import bei
    from apps.web.app import _ALL_TYPES
    from apps.web.routers import cartera, curva
    from apps.web.routers.panels_schema import PANELS
    assert "DUAL_DL_TAMAR" in _ALL_TYPES
    assert "DUAL_DL_TAMAR" in PANELS["tamar"][1]
    assert cartera._GRUPO["DUAL_DL_TAMAR"] == "TAMAR"
    assert "DUAL_DL_TAMAR" not in curva._CURVA_GROUPS["tamar"][1]
    assert "DUAL_DL_TAMAR" not in bei.DUAL_TAMAR
