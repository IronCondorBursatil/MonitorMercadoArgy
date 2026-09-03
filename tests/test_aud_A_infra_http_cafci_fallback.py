"""Hallazgo A-6: el fallback ArgentinaDatos del CAFCIProvider emitía todas sus
filas con `fondo_id=None`, y `unify_classes` (core/domain/fci/derive.py) agrupa
por `fondo_id` sin normalizar → TODO el universo colapsaba en `by_fondo[None]`:
un solo fondo, con `fondo` del primer registro, `vcp`/`fecha`/`cid` de OTRO
fondo (el de mayor AUM) y `aum` = suma de toda la industria.
"""

from core.domain.fci import ARD_FCI_ENDPOINTS
from core.domain.fci.derive import unify_classes
from core.infrastructure import cafci_provider as cp
from core.infrastructure.cafci_provider import CAFCIProvider

_ROWS = {
    "Mercado de Dinero": [{"fondo": "A MM", "vcp": "1.0", "fecha": "2026-09-01"}],
    "Renta Fija":        [{"fondo": "B RF", "vcp": "2.0", "fecha": "2026-09-01"}],
    "Renta Variable":    [{"fondo": "C RV", "vcp": "3.0", "fecha": "2026-09-01"}],
    "Renta Mixta":       [],
    "Retorno Total":     [],
}
_BY_URL = {url: _ROWS[cat] for cat, url in ARD_FCI_ENDPOINTS.items()}


class _Resp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        return None

    def json(self):
        return self._rows


def _patch(monkeypatch):
    monkeypatch.setattr(cp.httpx, "get", lambda url, **kw: _Resp(_BY_URL[url]))


def test_fallback_ard_emite_identidad_estable_por_fondo(monkeypatch):
    _patch(monkeypatch)
    funds = CAFCIProvider._fetch_ard_fallback()
    assert len(funds) == 3
    fids = [f["fondo_id"] for f in funds]
    assert all(fid for fid in fids), "fondo_id nulo → colapsa el universo"
    assert len(set(fids)) == 3, "ids no únicos"
    assert all(f["clase_id"] for f in funds)


def test_fallback_ard_no_colapsa_el_universo_en_unify_classes(monkeypatch):
    _patch(monkeypatch)
    funds = CAFCIProvider._fetch_ard_fallback()
    out = unify_classes(funds, {})
    assert len(out) == 3, f"todos los fondos colapsados en {len(out)} registro(s)"
    por_nombre = {f["fondo"]: f for f in out}
    assert set(por_nombre) == {"A MM", "B RF", "C RV"}
    # cada fondo conserva SU vcp (antes tomaba el de la clase de mayor AUM de otro)
    assert por_nombre["A MM"]["clases"][0]["vcp"] == 1.0
    assert por_nombre["C RV"]["clases"][0]["vcp"] == 3.0
    assert all(len(f["clases"]) == 1 for f in out)


def test_fallback_ard_id_es_deterministico(monkeypatch):
    """El fid siembra `reconstruct_hist` y lo usan FMAP/favoritos del front: no
    puede cambiar entre corridas."""
    _patch(monkeypatch)
    a = {f["fondo_nombre"]: f["fondo_id"] for f in CAFCIProvider._fetch_ard_fallback()}
    b = {f["fondo_nombre"]: f["fondo_id"] for f in CAFCIProvider._fetch_ard_fallback()}
    assert a == b
