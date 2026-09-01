"""Unificacion de nombres de emisor de ON (scripts/normalize_on_emisor.py)."""

import pytest

from scripts.normalize_on_emisor import ALIAS, canonico, raiz


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


# --------------------------------------------------------------------------- #
# C3: la forma societaria abreviada con puntos ("S.R.L.", "S.A.C.I.F.") tiene que
# caer igual que la escrita corrida ("SRL", "SACIF"). Antes se borraban los puntos
# POR ESPACIOS antes de recortar la sigla, asi que solo "S.A." quedaba limpio y el
# resto sobrevivia descosido ("S R L") partiendo al emisor en dos.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("a,b", [
    ("Plaza Logistica S.R.L.", "Plaza Logistica SRL"),
    ("Arcor S.A.I.C.", "ARCOR"),
    ("Generacion Mediterranea S.A.C.I.F.", "GENERACION MEDITERRANEA"),
    ("Petroquimica Comodoro Rivadavia S.A.I.C.F.", "PETROQUIMICA COMODORO RIVADAVIA"),
    ("Rizobacter Argentina S.A.U.", "RIZOBACTER ARGENTINA"),
    ("Newsan S.A.", "NEWSAN"),
])
def test_la_forma_societaria_con_puntos_se_recorta_igual_que_sin_puntos(a, b):
    assert raiz(a) == raiz(b)


def test_la_sigla_no_se_come_parte_del_nombre():
    """Recortar la forma societaria no puede amputar una palabra del emisor:
    'NEWSAN' NO es 'NEW' + 'SAN', y 'SAMSUNG' no termina en 'SA'."""
    assert raiz("NEWSAN") == "NEWSAN"
    assert raiz("SAMSUNG") == "SAMSUNG"


# --------------------------------------------------------------------------- #
# C4: ALIAS es la valvula de escape para la marca comercial que la raiz NO puede
# unificar sola (el backfill BYMA inyecto razones sociales largas junto a los
# nombres cortos preexistentes). El lookup tiene que funcionar tambien con las
# variantes sufijadas ("EDENOR - Clase 9", "IRSA S.A.").
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("a,b", [
    ("EDENOR", "Empresa Distribuidora y Comercializadora Norte S.A."),
    ("EDEMSA", "Empresa Distribuidora de Electricidad de Mendoza S.A."),
    ("IRSA", "IRSA INVERSIONES Y REPRESENTACIONES S.A."),
    ("MASTELLONE", "Mastellone Hermanos S.A."),
    ("PAN AMERICAN ENERGY", "PAN AMERICAN ENERGY, S.L. SUCURSAL ARGENTINA"),
    ("VISTA ENERGY", "VISTA ENERGY ARGENTINA S.A.U."),
    ("YPF LUZ", "YPF Energía Eléctrica S.A."),
])
def test_alias_unifica_marca_comercial_con_razon_social(a, b):
    assert raiz(a) == raiz(b)


@pytest.mark.parametrize("variante", [
    "EDENOR - Clase 9",     # sufijo de serie
    "Edenor S.A.",          # forma societaria + minusculas
    "EDENOR",
])
def test_el_alias_matchea_variantes_sufijadas_no_solo_el_string_exacto(variante):
    """El lookup viejo era `ALIAS.get(sn.upper())` sobre el nombre COMPLETO: con
    cualquier sufijo fallaba y el emisor volvia a partirse."""
    assert raiz(variante) == raiz("Empresa Distribuidora y Comercializadora Norte S.A.")


def test_el_canonico_del_grupo_aliasado_es_la_razon_social():
    """La marca comercial corta pierde contra la razon social larga (mas completa)."""
    assert canonico({"EDENOR", "Empresa Distribuidora y Comercializadora Norte S.A."}) \
        == "Empresa Distribuidora y Comercializadora Norte S.A."


def test_el_alias_es_idempotente():
    """raiz(raiz_destino) == raiz(marca): sin esto, correr el script dos veces
    renombraria en ping-pong."""
    for marca, razon in ALIAS.items():
        assert raiz(razon) == raiz(marca)
