"""Unificacion de nombres de emisor de ON (scripts/normalize_on_emisor.py)."""

import pytest

from scripts.normalize_on_emisor import canonico, raiz


@pytest.mark.parametrize("a,b", [
    ("YPF", "YPF S.A."),
    ("YPF - Clase XXXIX", "YPF S.A."),
    ("PAMPA ENERGIA", "PAMPA ENERGIA S.A"),
    ("TELECOM ARGENTINA - Clase 24", "TELECOM ARGENTINA S. A."),
    ("PLUSPETROL - Clase 5", "Pluspetrol S.A."),
    ("CAPEX", "CAPEX S.A."),
])
def test_variantes_de_la_misma_empresa_comparten_raiz(a, b):
    assert raiz(a) == raiz(b)


@pytest.mark.parametrize("a,b", [
    # YPF S.A. (petrolera, YM*) vs YPF Energia Electrica (generadora, YF*):
    # emisores DISTINTOS con creditos distintos. Fusionarlos seria un error.
    ("YPF S.A.", "YPF Energía Eléctrica S.A."),
    ("MSU ENERGY S.A.", "MSU S.A."),
    ("BANCO MACRO S.A.", "BANCO COMAFI S.A."),
])
def test_empresas_distintas_no_se_fusionan(a, b):
    assert raiz(a) != raiz(b)


def test_el_canonico_es_la_variante_mas_completa():
    assert canonico({"YPF", "YPF - Clase XXXIX", "YPF S.A."}) == "YPF S.A."
    assert canonico({"TGS", "TGS - Clase 4"}) == "TGS"


def test_es_idempotente():
    """Correrlo dos veces no cambia nada: el canonico es punto fijo."""
    grupo = {"YPF", "YPF - Clase XXXIX", "YPF S.A."}
    c = canonico(grupo)
    assert canonico({c}) == c
    assert raiz(c) == raiz("YPF")
