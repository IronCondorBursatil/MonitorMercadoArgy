"""Clasificación de ON por sector (criterio keyword-sobre-emisor)."""

import pytest

from core.domain.on_classification import (
    SECTORS, classify_sector, normalize_issuer, sector_meta,
)


@pytest.mark.parametrize("emisor,expected", [
    ("EDENOR", "Utilities (Luz / Gas)"),
    ("YPF S.A.", "Energía / Petróleo & Gas"),
    ("Vista Energy Argentina", "Energía / Petróleo & Gas"),
    ("Banco Galicia", "Servicios Financieros"),
    ("Tarjeta Naranja", "Servicios Financieros"),
    ("CRESUD", "Agro / Alimentos"),
    ("Mastellone Hnos", "Agro / Alimentos"),
    ("IRSA", "Real Estate"),
    ("Loma Negra", "Industrial / Maquinaria"),
    ("Aeropuertos Argentina 2000", "Infraestructura / Construcción"),
    ("TELECOM ARGENTINA", "Telecomunicaciones"),
    ("Telecom Argentina S.A. - Clase 23", "Telecomunicaciones"),
    ("Mercado Pago - Clase 4", "Servicios Financieros"),
    ("Scania Credit - Clase 2", "Servicios Financieros"),
    ("Petroquimica Comodoro Rivadavia S.A. - Clase S", "Energía / Petróleo & Gas"),
    ("TRANSPORTADORA DE GAS DEL SUR S.A.", "Energía / Petróleo & Gas"),  # nombre legal BYMA (sin sigla TGS)
])
def test_known_issuers_classify(emisor, expected):
    assert classify_sector(emisor) == expected


def test_unknown_issuer_falls_back_to_otros():
    assert classify_sector("Frobnicate Holdings SA") == "Otros"
    assert classify_sector("") == "Otros"
    assert classify_sector(None) == "Otros"  # type: ignore[arg-type]


def test_priority_utilities_before_energia():
    # EDEMSA es distribuidora (Utilities), aunque "energía" aparezca en su razón social.
    assert classify_sector("EDEMSA Energía Mendoza") == "Utilities (Luz / Gas)"


def test_substring_safety_ledesma_is_agro_not_utilities():
    # "LEDESMA" no debe matchear "EDESA" (Utilities); es Agro.
    assert classify_sector("Ledesma SAAI") == "Agro / Alimentos"


def test_normalization_is_accent_case_whitespace_insensitive():
    a = classify_sector("Pampa Energía")
    b = classify_sector("PAMPA ENERGIA")
    c = classify_sector("  pampa   energia  ")
    assert a == b == c == "Energía / Petróleo & Gas"
    assert normalize_issuer("Énérgía") == "ENERGIA"


def test_sectors_catalog_has_9_entries_including_otros():
    keys = [s.key for s in SECTORS]
    assert len(SECTORS) == 9
    assert keys[-1] == "Otros"
    assert keys[0] == "Energía / Petróleo & Gas"  # orden canónico
    assert "Telecomunicaciones" in keys
    # cada sector de keywords existe en SECTORS
    from core.domain.on_classification import SECTOR_KEYWORDS
    for label, _kws in SECTOR_KEYWORDS:
        assert label in {s.key for s in SECTORS}


def test_sector_meta_lookup():
    m = sector_meta("Servicios Financieros")
    assert m.short == "Serv. Financieros" and m.color.startswith("#")
    assert sector_meta("inexistente").key == "Otros"


def test_sector_for_uses_manual_override():
    """sector_for() respeta el override manual del ABM (categoría elegida); si no hay
    o es inválido, cae al match por emisor."""
    from types import SimpleNamespace

    from core.domain.on_classification import sector_for
    # override válido gana aunque el emisor matchee otro sector
    ypf = SimpleNamespace(short_name="YPF S.A.", sector_override="Servicios Financieros")
    assert sector_for(ypf) == "Servicios Financieros"
    # sin override → deduce del emisor
    assert sector_for(SimpleNamespace(short_name="YPF S.A.", sector_override=None)) == "Energía / Petróleo & Gas"
    # override basura → ignora y deduce
    assert sector_for(SimpleNamespace(short_name="EDENOR", sector_override="xx")) == "Utilities (Luz / Gas)"
