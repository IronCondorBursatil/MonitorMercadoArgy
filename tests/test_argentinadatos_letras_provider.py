"""`fetch_letras` y la FORMA de la respuesta de `/v1/finanzas/letras`.

Verificado contra la API viva el 2026-09-07: la respuesta pasó de una lista pelada a
un sobre `{"fechaActualizacion": ..., "letras": [...]}`. El provider tenía que
desenvolverlo; como no lo hacía, cada corrida horaria del loop terminaba en
"expected list, got dict", el planificador recibía una lista vacía y el guard del
60 % rechazaba el payload por "corte roto" — un diagnóstico falso durante semanas.
"""

import pytest

from core.infrastructure import argentinadatos_provider as ad


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


def _provider(monkeypatch, data):
    monkeypatch.setattr(ad.httpx, "get", lambda *_a, **_k: _Resp(data))
    return ad.ArgentinaDatosProvider()


def test_desenvuelve_el_sobre_letras_del_contrato_2026_09(monkeypatch):
    filas = [{"ticker": "S29E7", "fechaVencimiento": "2027-01-29", "precioArs": 100.5}]
    p = _provider(monkeypatch, {"fechaActualizacion": "2026-09-07T20:00:00", "letras": filas})
    assert p.fetch_letras() == filas


def test_sigue_aceptando_la_lista_pelada_del_contrato_viejo(monkeypatch):
    filas = [{"ticker": "S30X6", "fechaEmision": "2026-01-30",
              "fechaVencimiento": "2026-12-30", "tem": 2.5, "vpv": 117.5}]
    assert _provider(monkeypatch, filas).fetch_letras() == filas


@pytest.mark.parametrize("data", [
    {"fechaActualizacion": "x"},          # sobre sin `letras`
    {"letras": "no-es-lista"},            # sobre con otra cosa adentro
    "texto",
    42,
])
def test_cualquier_otra_forma_se_rechaza_y_devuelve_vacio(monkeypatch, caplog, data):
    """Un sobre sin `letras` (o con otra cosa adentro) no puede llegar al
    planificador como si fuera un payload: se avisa y se devuelve lo cacheado (nada)."""
    p = _provider(monkeypatch, data)
    assert p.fetch_letras() == []
    assert any("letras" in r.getMessage().lower() for r in caplog.records)
