"""`core/domain/missing.py` — "un 0 / ≤0 de una fuente externa es DATO AUSENTE, no un valor".

La regla se redescubrió tres veces antes de tener un nombre (agents.md §0.1.9): el floor de
precios (`provider_hub`: "un 0 no es dato"), `ccp <= 0` en `fci_history` (fabricaba
suscripciones fantasma por el patrimonio entero) y `tem: 0` en `letras_sync` (persistía una
tasa del 0 %). Ahora vive en UN helper y los tres bordes lo usan; este test fija el contrato.
"""

from __future__ import annotations

import math

import pytest

from core.domain.missing import es_dato_ausente, valor_o_none


@pytest.mark.parametrize("x", [None, "", "   ", 0, 0.0, -1, -0.5, "0", "0.0", "-3",
                               float("nan"), "nan", "abc", [], {}])
def test_es_dato_ausente_para_lo_que_no_es_un_valor(x):
    assert es_dato_ausente(x) is True


@pytest.mark.parametrize("x", [1, 0.01, 158.2, "1.5", " 42 ", 1e-9, True])
def test_no_es_ausente_para_un_valor_positivo(x):
    assert es_dato_ausente(x) is False


@pytest.mark.parametrize("x,esperado", [
    (None, None), ("", None), (0, None), ("0", None), (-2.5, None), ("nan", None),
    (float("nan"), None), ("abc", None), (3, 3.0), ("2.75", 2.75), (" 7 ", 7.0),
])
def test_valor_o_none(x, esperado):
    out = valor_o_none(x)
    assert out == esperado
    if out is not None:
        assert isinstance(out, float) and not math.isnan(out)


def test_los_tres_bordes_usan_el_helper():
    """Si un borde vuelve a escribir la regla a mano, se pierde el punto de tener una sola."""
    from pathlib import Path

    raiz = Path(__file__).resolve().parent.parent / "core" / "infrastructure"
    for archivo in ("provider_hub.py", "fci_history.py", "letras_sync.py"):
        src = (raiz / archivo).read_text(encoding="utf-8")
        assert "core.domain.missing" in src, f"{archivo} no importa core.domain.missing"
