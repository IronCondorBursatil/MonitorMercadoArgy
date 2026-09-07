"""Leg `_TF` del popup: sólo existe para TAMAR PURO/DUAL (apps/web/bond_detail).

`_apply_leg` resuelve `<ticker>_TF` sustituyendo el provider de índices por `_ZeroTamar`,
un stub que implementa SOLO `get_tamar` (TAMAR=0 → `max(TAMAR, floor) = floor`). No es un
`IndicesProvider` completo: no tiene `get_cer`. Hasta este fix `_resolve_instrument_and_leg`
aceptaba el sufijo sobre CUALQUIER base existente, así que `TX26_TF` (CER) o `TXMJ8_TF`
(DUAL_CER_TAMAR) pasaban el guard `_is_cer_type`, el pricing pedía `get_cer` y reventaba
con AttributeError → 500 en `GET /bond/{t}/detail`, `/cer` y `POST /metrics`. Ningún
template emite links `_TF`: se llega sólo por URL tipeada, pero era un 500 no controlado.

Decisión (entre "rechazar el leg" y "darle `get_cer` a `_ZeroTamar` delegando al provider
real"): se RECHAZA. Delegar hubiera "funcionado" pero devolviendo un CER disfrazado de
"Tasa fija (leg TF)" — un leg sin sentido financiero servido como si existiera. El leg
queda definido exactamente donde TAMAR es el único índice que consume el pricing: TAMAR
PURO y DUAL (allowlist, no denylist de CER/DUAL_CER_TAMAR: un tipo nuevo que pida
`get_cer` nace cubierto). Para el resto, `_TF` es un ticker inexistente → 404 /
`is_cer: False`, igual que `NOPE123`.

Providers stub (sin red), compartidos con test_bond_detail. Mutación probada: sin el guard
de `_resolve_instrument_and_leg` los tests de CER levantan AttributeError.
"""

import pytest
from fastapi.testclient import TestClient

from apps.web import bond_detail
from apps.web.app import app
from core.infrastructure.db.catalog_repository import CatalogRepository
from tests.test_bond_detail import _StubFx, _StubIndices, _StubProvider


@pytest.fixture(scope="module")
def repo():
    return CatalogRepository(auto_seed=True)


def _alive(repo):
    settle = bond_detail._resolve_ref(1)
    return [i for i in repo.get_all_instruments()
            if i.maturity_date and i.maturity_date > settle]


def _needs_cer(repo):
    """Todos los vivos cuyo pricing pide `get_cer`: CER (incl. LECER/BONCER ZC) y TXMJ*."""
    out = [i.ticker for i in _alive(repo) if i.is_cer or i.is_dual_cer_tamar]
    assert out, "no hay CER ni DUAL_CER_TAMAR vivos en el catálogo"
    return out


def _tamar_only(repo):
    """Todos los vivos donde el leg TF está definido: TAMAR PURO y DUAL."""
    out = [i.ticker for i in _alive(repo) if i.is_tamar_puro or i.is_dual_tamar]
    assert out, "no hay TAMAR PURO/DUAL vivos en el catálogo"
    return out


def _one(repo, pred):
    tk = next((i.ticker for i in _alive(repo) if pred(i)), None)
    assert tk, "el catálogo no tiene un instrumento vivo para este caso"
    return tk


def _args():
    return _StubProvider(), _StubIndices(), _StubFx()


# --------------------------------------------------------------------------- #
# Negativo: `_TF` sobre un bono que precia con CER no existe (nunca AttributeError).
# --------------------------------------------------------------------------- #

def test_get_bond_detail_tf_sobre_cer_devuelve_none(repo):
    for tk in _needs_cer(repo):
        assert bond_detail.get_bond_detail(f"{tk}_TF", repo, *_args(),
                                           settlement_lag=1) is None, tk


def test_calculate_tf_sobre_cer_devuelve_none(repo):
    tk = _one(repo, lambda i: i.is_cer)
    assert bond_detail.calculate(f"{tk}_TF", repo, *_args(), mode="from_price",
                                 price=100.0, settlement_lag=1) is None


def test_cer_projection_tf_sobre_cer_is_cer_false(repo):
    # Mismo shape que un ticker inexistente (ver test_cer_projection_unknown_ticker_is_cer_false).
    for tk in (_one(repo, lambda i: i.is_cer), _one(repo, lambda i: i.is_dual_cer_tamar)):
        d = bond_detail.cer_projection(f"{tk}_TF", repo, *_args(), price_dirty=100.0)
        assert d == {"is_cer": False, "months": [], "scenarios": [], "default_unif": None}, tk


def test_router_tf_sobre_cer_no_es_500():
    repo = CatalogRepository(auto_seed=True)
    tk = _one(repo, lambda i: i.is_cer)
    with TestClient(app) as c:
        r = c.get(f"/bond/{tk}_TF/detail")
        assert r.status_code == 404
        assert "Instrumento no encontrado" in r.text
        # El cajón CER no 404ea: renderiza el aviso de "no es CER" (leer el router).
        r = c.get(f"/bond/{tk}_TF/cer?lag=1&price=100")
        assert r.status_code == 200
        assert "Este instrumento no es CER." in r.text
        r = c.post(f"/bond/{tk}_TF/metrics", data={"settlement_lag": "1", "price": "100"})
        assert r.status_code == 200
        assert "No calculable" in r.text


# --------------------------------------------------------------------------- #
# Positivo: el leg sigue vivo donde corresponde (PURO/DUAL), por función y por router.
# --------------------------------------------------------------------------- #

def test_get_bond_detail_tf_sobre_tamar_sigue_funcionando(repo):
    for tk in _tamar_only(repo):
        d = bond_detail.get_bond_detail(f"{tk}_TF", repo, *_args(), settlement_lag=1)
        assert d is not None, tk
        assert d["ticker"] == f"{tk}_TF"
        assert d["meta"]["ticker"] == f"{tk}_TF"
        assert d["meta"]["cupon"].startswith("Tasa fija"), d["meta"]["cupon"]
        assert d["meta"]["is_tamar_family"] is False   # TF se muestra como tasa fija


def test_router_tf_sobre_dual_renderiza():
    repo = CatalogRepository(auto_seed=True)
    tk = _one(repo, lambda i: i.is_dual_tamar)
    with TestClient(app) as c:
        r = c.get(f"/bond/{tk}_TF/detail")
        assert r.status_code == 200
        assert f"{tk}_TF" in r.text


def test_resolver_rechaza_tf_fuera_de_tamar_y_acepta_adentro(repo):
    """El guard vive en `_resolve_instrument_and_leg` (única puerta de los 4 endpoints)."""
    idx = _StubIndices()
    cer = _one(repo, lambda i: i.is_cer)
    dual = _one(repo, lambda i: i.is_dual_tamar)
    assert bond_detail._resolve_instrument_and_leg(f"{cer}_TF", repo, idx) is None
    resolved = bond_detail._resolve_instrument_and_leg(f"{dual}_TF", repo, idx)
    assert resolved is not None
    base, inst, indices_eff, leg, ticker_u = resolved
    assert (base, leg, ticker_u) == (dual, "TF", f"{dual}_TF")
    assert isinstance(indices_eff, bond_detail._ZeroTamar)
    # El leg no altera el base: sin sufijo se resuelve con el provider real.
    plain = bond_detail._resolve_instrument_and_leg(cer, repo, idx)
    assert plain is not None and plain[2] is idx and plain[3] is None
