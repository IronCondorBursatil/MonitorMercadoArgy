"""DUAL dólar-linked/TAMAR (`DUAL_DL_TAMAR`, caso TMVE8).

Spec: docs/superpowers/specs/2026-09-08-dual-dl-tamar-design.md. Payoff a vencimiento =
max(riel TAMAR capitalizado mensual desde la emisión, 100 × FX / TC inicial); TEA nominal en
pesos; el dólar es el del settle, sin proyectar.
"""
from __future__ import annotations

from datetime import date

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
