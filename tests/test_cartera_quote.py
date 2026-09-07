"""`_quote` / `_fx_rates` de apps/web/routers/cartera.py: tolerantes de verdad.

`_quote` promete "None si falla", pero el `try` envolvía sólo la llamada `fn()`: la
comparación `v > 0` quedaba afuera, así que una punta NO numérica (un provider/mock que
devuelve "n/a", un dict, una lista) tiraba TypeError en `_fx_rates` → 500 en `/cartera`.
Y un `True` pasaba como precio (`True > 0`).

El contrato que fija este archivo: sólo un número real (int/float, NO bool) positivo es
una cotización; cualquier otra cosa degrada a None y `_fx_rates` cae al mayorista como
documenta su docstring. Mutación probada: con el `v > 0` fuera del `isinstance`, los
tests de "n/a"/dict levantan TypeError y el de bool devuelve True.
"""

from fastapi.testclient import TestClient

from apps.web.app import app
from apps.web.deps import get_fx
from apps.web.routers import cartera


class _Fx:
    """Provider FX de una sola punta configurable (MEP); las otras no existen."""

    def __init__(self, mep):
        self._mep = mep

    def get_mep_venta(self):
        return self._mep


class _FxRaro:
    """MEP no numérico, CCL ausente, mayorista sano: el caso del hallazgo."""

    def get_mep_venta(self):
        return "n/a"

    def get_ccl_venta(self):
        return None

    def get_mayorista_venta(self):
        return 1000.0


def test_quote_solo_acepta_numeros_reales_positivos():
    for raro in ("n/a", {"venta": 1.0}, [1.0], (1.0,), True, False, 0, 0.0, -5, None,
                 float("nan")):
        assert cartera._quote(_Fx(raro), "get_mep_venta") is None, repr(raro)
    assert cartera._quote(_Fx(1234.5), "get_mep_venta") == 1234.5
    assert cartera._quote(_Fx(7), "get_mep_venta") == 7
    # Método inexistente o que levanta: None, sin propagar.
    assert cartera._quote(_Fx(1.0), "get_ccl_venta") is None
    assert cartera._quote(None, "get_mep_venta") is None


def test_fx_rates_degrada_al_mayorista_con_punta_no_numerica():
    # MEP "n/a" → None; CCL None → None; ambos caen al mayorista (docstring de _fx_rates).
    assert cartera._fx_rates(_FxRaro()) == (1000.0, 1000.0)


def test_cartera_page_no_es_500_con_fx_raro():
    app.dependency_overrides[get_fx] = lambda: _FxRaro()
    try:
        with TestClient(app) as c:
            r = c.get("/cartera")
            assert r.status_code == 200
            assert "POSICIONES" in r.text
    finally:
        app.dependency_overrides.pop(get_fx, None)
