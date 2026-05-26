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
    tk = repo.get_all_instruments()[0].ticker
    c = bond_detail.calculate(tk, repo, _StubProvider(), _StubIndices(), _StubFx(),
                              mode="from_tir", tir=0.30, settlement_lag=1)
    assert c is None or "price_dirty" in c
