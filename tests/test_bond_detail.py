"""Regresión del popup de detalle (apps/web/bond_detail).

Cubre el bug del 'WIP checkpoint': `get_bond_detail` usaba `leg`/`ticker_u` sin
definirlos (NameError → HTTP 500). Providers stub → sin red.
"""

import pytest

from apps.web import bond_detail
from core.infrastructure.db.catalog_repository import CatalogRepository


class _StubProvider:
    def fetch_snapshots(self, tickers):
        return {}  # sin snapshot → price None (no toca la red)


class _StubIndices:
    def get_cer(self, target=None):
        return 100.0

    def get_tamar(self, target=None):
        return 30.0


class _StubFx:
    def get_mayorista_venta(self):
        return 1100.0


@pytest.fixture(scope="module")
def repo():
    return CatalogRepository(auto_seed=True)


def test_get_bond_detail_returns_dict_for_all_types(repo):
    # Un ticker representativo por cada instrument_type del catálogo.
    seen = {}
    for inst in repo.get_all_instruments():
        seen.setdefault(inst.instrument_type, inst.ticker)
    assert seen, "catálogo vacío"
    for itype, tk in seen.items():
        d = bond_detail.get_bond_detail(tk, repo, _StubProvider(), _StubIndices(), _StubFx(),
                                        settlement_lag=1)
        assert d is not None, f"{tk} ({itype}) devolvió None"
        assert d["ticker"] == tk
        assert {"meta", "metrics", "cashflows", "market"} <= set(d)


def test_get_bond_detail_unknown_ticker_returns_none(repo):
    assert bond_detail.get_bond_detail("NOPE123", repo, _StubProvider(),
                                       _StubIndices(), _StubFx()) is None


def test_calculate_from_tir_does_not_raise(repo):
    # Bono vivo (no `[0]` ciego): un vencido descontaría flujos pasados.
    settle = bond_detail._resolve_ref(1)
    tk = next((i.ticker for i in repo.get_all_instruments()
               if i.maturity_date and i.maturity_date > settle and i.cashflows),
              repo.get_all_instruments()[0].ticker)
    c = bond_detail.calculate(tk, repo, _StubProvider(), _StubIndices(), _StubFx(),
                              mode="from_tir", tir=0.30, settlement_lag=1)
    assert c is None or "price_dirty" in c


class _StubRem:
    """REM provider stub: path mensual + YoY (sin red)."""

    def get_monthly_path(self):
        from datetime import date as _d
        return {_d(2026, 6, 1): 0.020, _d(2026, 7, 1): 0.019, _d(2026, 8, 1): 0.018}

    def get_next_12m_yoy(self):
        return 0.25


def _a_cer_ticker(repo):
    """Un CER VIVO (no vencido) con cer_base y flujos futuros al settle.

    Robusto al paso del tiempo: tomar 'el primer CER' tomaba X29Y6, un LECER que
    venció 2026-05-29 → sin flujos futuros, la proyección/escenarios salen vacíos
    (comportamiento correcto para un bono vencido, pero inútil para testear el
    eco de 'Mi escenario'). Filtramos por maturity > settle y flujos futuros."""
    settle = bond_detail._resolve_ref(1)
    return next(
        (i.ticker for i in repo.get_all_instruments()
         if bond_detail._is_cer_type(i.instrument_type) and i.cer_base
         and i.maturity_date and i.maturity_date > settle
         and i.get_future_cashflows(settle)),
        None,
    )


def test_cer_projection_cer_bond_has_months_and_scenarios(repo):
    cer_tk = _a_cer_ticker(repo)
    assert cer_tk, "no hay CER en el catálogo"
    sendero = [
        {"mes": "jun-26", "rem_mensual": 0.020, "bei_mensual": 0.022},
        {"mes": "jul-26", "rem_mensual": 0.019, "bei_mensual": 0.021},
    ]
    d = bond_detail.cer_projection(
        cer_tk, repo, _StubProvider(), _StubIndices(), _StubFx(),
        price_dirty=100.0, settlement_lag=1, bei_sendero=sendero, rem_provider=_StubRem(),
    )
    assert d["is_cer"] is True
    assert d["months"], "sin meses proyectados"
    m0 = d["months"][0]
    assert {"label", "ym", "cer_proj", "rem", "bei"} <= set(m0)
    assert m0["rem"] is not None  # primera ref REM presente
    assert isinstance(d["scenarios"], list)


def test_cer_projection_custom_monthly_echoed_into_scenarios(repo):
    cer_tk = _a_cer_ticker(repo)
    assert cer_tk
    d = bond_detail.cer_projection(
        cer_tk, repo, _StubProvider(), _StubIndices(), _StubFx(),
        price_dirty=100.0, settlement_lag=1, rem_provider=_StubRem(),
        custom_infl_monthly=0.03,
    )
    labels = [r["label"] for r in d["scenarios"]]
    assert "Mi escenario" in labels


def test_cer_projection_non_cer_returns_is_cer_false(repo):
    non = next(
        (i.ticker for i in repo.get_all_instruments()
         if not bond_detail._is_cer_type(i.instrument_type)),
        None,
    )
    if non is None:
        return
    d = bond_detail.cer_projection(non, repo, _StubProvider(), _StubIndices(), _StubFx(),
                                   price_dirty=100.0, rem_provider=_StubRem())
    assert d["is_cer"] is False


def test_cer_projection_unknown_ticker_is_cer_false(repo):
    d = bond_detail.cer_projection("NOPE123", repo, _StubProvider(), _StubIndices(), _StubFx(),
                                   price_dirty=100.0, rem_provider=_StubRem())
    assert d["is_cer"] is False
