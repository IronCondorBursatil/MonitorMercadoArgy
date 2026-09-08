"""DUAL dólar-linked/TAMAR (`DUAL_DL_TAMAR`, caso TMVE8).

Spec: docs/superpowers/specs/2026-09-08-dual-dl-tamar-design.md. TMVE8 cotiza en **pesos por
100 VN denominados en USD** (Data912 2026-09-08: c=139.680, como los dólar-linked TZVD8 /
D31M7 y NO como los duales TAMAR, que van por 100 VN en pesos), así que las dos patas del
payoff van en esa escala: max(TC inicial × riel TAMAR capitalizado mensual desde la emisión,
100 × FX). TEA nominal en pesos; el dólar es el del settle, sin proyectar.
"""
from __future__ import annotations

from datetime import date

import pytest

from core.domain.instrument_groups import BOND_TYPES, DUAL_DL, DUAL_TAMAR, is_known_type
from core.domain.models import Cashflow, Instrument

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
    hoja Dólar Linked; 0 o vacío = dato ausente en las dos.

    La **coma decimal la tolera sólo el motor** (`catalog_repository._fx_base` hace
    `replace(",", ".")`, porque la celda puede venir del Excel); `build_instrument` va por
    `_opt_float` → `float(val)` pelado, y no hace falta: el `<input type="number">` del form
    manda punto."""
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
# Precio de referencia en la escala real del papel: pesos por 100 VN USD (≈ 120 × 1300).
_PRICE = 156_000.0


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
    tamar.py, para que el test no dependa de lo que pinea. Es la capitalización **per-100
    USD**: el payoff en pesos la multiplica por el TC inicial (`_FX_BASE`)."""
    return 100.0 * (1.0 + tamar_tem(_TNA / 100.0)) ** (days_30_360(_EMISION, _VTO) / 30.0)


def _payoff_tamar_a_mano():
    """Riel TAMAR en la escala del precio: pesos por 100 VN USD = TC inicial × capitalización."""
    return _FX_BASE * _riel_tamar_a_mano()


def test_registry_rutea_el_tipo_nuevo_sin_mover_a_los_vecinos():
    assert isinstance(strategy_for(_inst()), DualDlTamarStrategy)
    assert isinstance(strategy_for(_inst(instrument_type="PURO")), TamarStrategy)
    assert isinstance(strategy_for(_inst(instrument_type="DUAL")), TamarStrategy)
    assert isinstance(strategy_for(_inst(instrument_type="DUAL_CER_TAMAR", cer_base=700.0)),
                      DualCerTamarStrategy)
    assert isinstance(strategy_for(_inst(instrument_type="DOLAR_LINKED")), DolarLinkedStrategy)


def test_gana_el_riel_tamar_cuando_el_dolar_no_alcanza():
    """FX 1500 → riel DL 150.000 < riel TAMAR (1300 × 156,1 ≈ 202.900): paga el riel TAMAR,
    exactamente el de la fórmula BONTE escalado por el TC inicial."""
    ctx = PricingContext(settle=_SETTLE, indices=_Idx(), fx=_Fx(1500.0))
    payoff = dual_dl_tamar_payoff_at(_inst(), _SETTLE, ctx)
    assert payoff == pytest.approx(_payoff_tamar_a_mano(), rel=1e-12)
    assert payoff == pytest.approx(
        _FX_BASE * tamar_dual_payoff_at(_inst(), _SETTLE, _Idx(), to_date=_VTO))
    assert payoff > 100.0 * 1500.0


def test_gana_el_riel_dl_cuando_el_dolar_supera_la_capitalizacion():
    """FX 2600 → riel DL 260.000 > riel TAMAR (≈202.900): paga 100 USD al dólar del settle,
    sin proyectarlo."""
    ctx = PricingContext(settle=_SETTLE, indices=_Idx(), fx=_Fx(2600.0))
    assert dual_dl_tamar_payoff_at(_inst(), _SETTLE, ctx) == pytest.approx(260_000.0)


def test_un_fx_cero_es_dato_ausente_y_cae_al_a3500():
    """Un 0 de dolarapi NO es un tipo de cambio (invariante «0 = dato ausente»): sin fixing
    detrás el payoff es None, y con A3500 se precia con 1500 igual que el dólar vivo."""
    ctx_sin = PricingContext(settle=_SETTLE, indices=_Idx(), fx=_Fx(1500.0))
    assert dual_dl_tamar_payoff_at(_inst(), _SETTLE, ctx_sin, fx_rate=0.0) is None
    assert dual_dl_tamar_payoff_at(_inst(), _SETTLE,
                                   PricingContext(settle=_SETTLE, indices=_Idx(),
                                                  fx=_Fx(0.0))) is None
    con_a3500 = PricingContext(settle=_SETTLE, indices=_IdxConA3500(), fx=_Fx(0.0))
    assert (dual_dl_tamar_payoff_at(_inst(), _SETTLE, con_a3500)
            == pytest.approx(dual_dl_tamar_payoff_at(
                _inst(), _SETTLE,
                PricingContext(settle=_SETTLE, indices=_Idx(), fx=_Fx(1500.0)))))


def test_vtec_es_el_max_de_rieles_devengado_al_settle():
    """Al settle el riel TAMAR lleva ~1,3 meses (1300 × ≈103 = ≈134.200) y el DL vale
    150.000: manda el DL. Antes de la emisión, 100 USD al TC inicial = 130.000."""
    fx = _Fx(1500.0)
    vt = FinancialEngine.calculate_technical_value(_snap(_inst(), _PRICE), _Idx(), fx,
                                                   ref_date=_SETTLE)
    tamar_al_settle = tamar_dual_payoff_at(_inst(), _SETTLE, _Idx(), to_date=_SETTLE)
    assert tamar_al_settle is not None and 100.0 < tamar_al_settle < 110.0
    assert vt == pytest.approx(max(_FX_BASE * tamar_al_settle, 100.0 * 1500.0))
    assert vt == pytest.approx(150_000.0)
    pre = FinancialEngine.calculate_technical_value(_snap(_inst(), _PRICE), _Idx(), fx,
                                                    ref_date=date(2026, 7, 1))
    assert pre == pytest.approx(100.0 * _FX_BASE)


def test_tir_es_tea_nominal_contra_el_payoff_y_md_usa_m12():
    inst = _inst()
    fx = _Fx(1500.0)
    tir = FinancialEngine.calculate_tir(_snap(inst, _PRICE), _Idx(), fx, settle_date=_SETTLE)
    years = inst.year_fraction_to(_VTO, _SETTLE)
    assert tir == pytest.approx((_payoff_tamar_a_mano() / _PRICE) ** (1.0 / years) - 1.0,
                                rel=1e-9)
    # Escala sana: un papel de 156.000 contra un payoff de ~202.900 a 1,4 años no rinde
    # miles por uno (eso es lo que devolvía la escala per-100 pesos antes de la corrección).
    assert 0.0 < tir < 0.50
    md = FinancialEngine.calculate_duration(_snap(inst, _PRICE), tir, settle_date=_SETTLE)
    assert md == pytest.approx(years / (1.0 + tir) ** (1.0 / 12.0), rel=1e-9)


@pytest.mark.parametrize("fx_val", [1500.0, 2600.0])
@pytest.mark.parametrize("price", [120_000.0, 156_000.0, 200_000.0])
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
    assert FinancialEngine.calculate_tir(_snap(inst, _PRICE), _Idx(), fx, settle_date=_SETTLE) is None
    assert FinancialEngine.calculate_technical_value(_snap(inst, _PRICE), _Idx(), fx,
                                                     ref_date=_SETTLE) is None
    # Tampoco antes de la emisión: el V.Téc pre-emisión son 100 USD al TC inicial, y sin TC
    # inicial no hay escala en pesos que inventar.
    assert FinancialEngine.calculate_technical_value(_snap(inst, _PRICE), _Idx(), fx,
                                                     ref_date=date(2026, 7, 1)) is None
    assert FinancialEngine.price_from_tir(_snap(inst, _PRICE), 0.3, _Idx(), fx,
                                          settle_date=_SETTLE) is None


def test_sin_dolar_vivo_cae_al_a3500_del_bcra_y_sin_ninguno_devuelve_none():
    inst = _inst()
    con_a3500 = FinancialEngine.calculate_tir(_snap(inst, _PRICE), _IdxConA3500(), _FxCaido(),
                                              settle_date=_SETTLE)
    vivo = FinancialEngine.calculate_tir(_snap(inst, _PRICE), _Idx(), _Fx(1500.0),
                                         settle_date=_SETTLE)
    assert con_a3500 == pytest.approx(vivo)             # mismo dólar (1500) por otra vía
    assert FinancialEngine.calculate_tir(_snap(inst, _PRICE), _Idx(), _FxCaido(),
                                         settle_date=_SETTLE) is None
    assert FinancialEngine.calculate_tir(_snap(inst, _PRICE), _Idx(), None,
                                         settle_date=_SETTLE) is None


def test_el_forecast_tamar_mueve_solo_el_riel_tamar():
    inst = _inst()
    manda_tamar = _Fx(1500.0)
    base = FinancialEngine.calculate_tir(_snap(inst, _PRICE), _Idx(), manda_tamar,
                                         settle_date=_SETTLE)
    alto = FinancialEngine.calculate_tir(_snap(inst, _PRICE), _Idx(), manda_tamar,
                                         settle_date=_SETTLE, tamar_forecast=0.60)
    assert alto > base
    # FX bien arriba de 2600: a forecast=0.60 el riel TAMAR sube a ~233 per-100 (blend
    # pasado/futuro de avg_tamar_tna sobre 18 meses) = ~302.900 pesos — con 2600 (riel DL
    # 260.000) el riel TAMAR forecast lo superaría y el test dejaría de aislar el riel DL.
    # 4000 → riel DL 400.000, con margen.
    manda_dl = _Fx(4000.0)
    assert (FinancialEngine.calculate_tir(_snap(inst, _PRICE), _Idx(), manda_dl,
                                          settle_date=_SETTLE, tamar_forecast=0.60)
            == pytest.approx(FinancialEngine.calculate_tir(_snap(inst, _PRICE), _Idx(),
                                                           manda_dl, settle_date=_SETTLE)))


def test_vencido_cae_al_camino_general():
    """Un papel vencido NO se precia por la fórmula cerrada: el guard `_vivo` lo manda a
    `VanillaStrategy`, que descuenta los flujos FUTUROS al settle. Con un flujo REAL ya
    pagado (2026-06-30 < settle) no queda ninguno → todo None.

    Lo que discrimina el guard es `price_from_tir`: sin `_vivo` descontaría el payoff cerrado
    (capitalización TAMAR de 23 meses × TC inicial, ≈2,5 M) con `años` NEGATIVO y devolvería
    un número; por el camino vanilla `metrics.vanilla_pv` no tiene flujos y devuelve None.
    (En `tir` los dos caminos dan None —el cerrado por `años <= 0`—, así que esa aserción
    sola no discriminaría.)"""
    inst = _inst(maturity_date=date(2026, 6, 30), emission_date=date(2024, 7, 31),
                 cashflows=(Cashflow(date=date(2026, 6, 30), amortization=100.0,
                                     interest=0.0),))
    fx = _Fx(1500.0)
    assert FinancialEngine.calculate_tir(_snap(inst, _PRICE), _Idx(), fx,
                                         settle_date=_SETTLE) is None
    assert FinancialEngine.price_from_tir(_snap(inst, _PRICE), 0.30, _Idx(), fx,
                                          settle_date=_SETTLE) is None
    assert FinancialEngine.calculate_duration(_snap(inst, _PRICE), 0.30,
                                              settle_date=_SETTLE) is None


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
