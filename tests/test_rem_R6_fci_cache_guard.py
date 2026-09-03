"""Remediación lote R6 (FCI) — el guard de "no memoizar un dataset degradado" también
mira el MEP.

Mitad faltante del hallazgo 7: `get_fci_dataset` memoiza por (corte CAFCI, día) y solo
condicionaba el guard a `if aum_index:`. Si el primer build del día agarra el cache de FX
frío o dolarapi caído, `macro['mep_now']` queda None y ese dataset degradado se congela
**hasta la medianoche siguiente**. Con el fix del cliente eso ya no miente en silencio,
pero rompe visiblemente: el lente 'USD @MEP' de la cuotaparte queda deshabilitado y los
fondos con clases en dólares salen del agregado de la vista Flujos ("sin MEP para
convertir") todo el día, aunque dolarapi vuelva a los 5 minutos.
"""

import apps.web.fci_service as fci_service

_PARSED = {
    "meta": {"fecha_base": "2026-08-31", "generated_at": "2026-08-31T20:00:00-03:00"},
    "funds": [{"fondo_id": 1, "clase_id": 11, "fondo_nombre": "Test MM",
               "clase_nombre": "Test MM - Clase A", "moneda": "Peso Argentina",
               "tipo_renta": "Mercado de Dinero", "vcp": 100.0,
               "fecha_valor": "2026-08-31", "rend": {}}],
}
_ARD = [{"fondo": "Test MM - Clase A", "patrimonio": 1e9, "ccp": 1e7}]


class _Cafci:
    _dataset = _PARSED

    def _ensure_loaded(self):
        pass


class _Idx:
    def cer_series(self):
        return {}

    def a3500_series(self):
        return {}


class _Fx:
    def __init__(self, mep):
        self.mep = mep

    def get_mep_venta(self):
        return self.mep


def _get(fx, monkeypatch):
    monkeypatch.setattr(fci_service, "fetch_ard_fci_rows", lambda: list(_ARD))
    return fci_service.get_fci_dataset(_Cafci(), _Idx(), fx)


def test_no_memoiza_el_dataset_sin_mep(monkeypatch):
    """dolarapi caído en el primer build: NO se cachea → la visita siguiente reintenta y,
    cuando el FX vuelve, el panel se arregla solo (sin esperar al corte del día siguiente)."""
    fci_service.clear_cache()
    try:
        frio = _get(_Fx(None), monkeypatch)
        assert frio["meta"]["macro"]["mep_now"] is None
        assert frio["meta"]["n_aum_real"] == 1, "el AUM sí estaba: el guard viejo cacheaba"

        caliente = _get(_Fx(1400.0), monkeypatch)
        assert caliente["meta"]["macro"]["mep_now"] == 1400.0, (
            "se sirvió el dataset degradado memoizado")
        assert caliente is not frio
    finally:
        fci_service.clear_cache()


def test_memoiza_cuando_el_dataset_esta_sano(monkeypatch):
    """El camino bueno sigue cacheando: mismo objeto (de eso depende el cache de bytes
    gzippeados del router, que se clavea por identidad)."""
    fci_service.clear_cache()
    try:
        uno = _get(_Fx(1400.0), monkeypatch)
        dos = _get(_Fx(1400.0), monkeypatch)
        assert uno is dos
    finally:
        fci_service.clear_cache()


def test_sigue_sin_memoizar_sin_aum(monkeypatch):
    """El guard original (ArgentinaDatos caído → aum_index vacío) no se perdió."""
    fci_service.clear_cache()
    try:
        monkeypatch.setattr(fci_service, "fetch_ard_fci_rows", lambda: [])
        a = fci_service.get_fci_dataset(_Cafci(), _Idx(), _Fx(1400.0))
        assert a["meta"]["n_aum_real"] == 0
        b = _get(_Fx(1400.0), monkeypatch)
        assert b is not a and b["meta"]["n_aum_real"] == 1
    finally:
        fci_service.clear_cache()
