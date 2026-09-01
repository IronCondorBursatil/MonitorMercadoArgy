"""Clasificación de ON por sector (criterio keyword-sobre-emisor)."""

import re
from pathlib import Path

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


def test_sectors_catalog_orden_y_unicidad():
    # Sin numero fijo a proposito: el catalogo CRECE (se agregan sectores cuando
    # entra un emisor que no encaja) — por eso el nombre del test no clava un tamaño.
    # Lo que no puede cambiar es el invariante: 'Otros' cierra siempre la lista
    # (bucket de descarte) y Energia la abre.
    keys = [s.key for s in SECTORS]
    assert len(keys) == len(set(keys)), "hay sectores duplicados"
    assert keys[-1] == "Otros"
    assert keys[0] == "Energía / Petróleo & Gas"  # orden canónico
    assert "Telecomunicaciones" in keys
    # cada sector de keywords existe en SECTORS
    from core.domain.on_classification import SECTOR_KEYWORDS
    for label, _kws in SECTOR_KEYWORDS:
        assert label in {s.key for s in SECTORS}


def test_sectors_js_es_espejo_exacto_del_catalogo_python():
    """`apps/web/on_src/sectors.js` es el fallback de arranque del cliente y este módulo
    es la fuente de verdad (el server manda el catálogo real en /on/data::sectors_meta).
    Si divergen, el panel arranca pintando un sector con el color/orden equivocado —o
    directamente sin él— hasta que llega el fetch. Nadie ataba ese espejo: acá se ata.
    Agregar un sector de un solo lado (o reordenarlo) pone este test en rojo."""
    js_path = (Path(__file__).resolve().parent.parent
               / "apps" / "web" / "on_src" / "sectors.js")
    js = js_path.read_text(encoding="utf-8")
    # basta una regex sobre `key: "..."`: el archivo es una lista literal, no JS vivo.
    js_keys = re.findall(r'key:\s*"([^"]+)"', js)
    assert js_keys, f"no se parseó ninguna key de {js_path}"
    assert js_keys == [s.key for s in SECTORS], (
        "sectors.js quedó desfasado de SECTORS (mismo conjunto Y mismo orden): "
        f"js={js_keys} vs py={[s.key for s in SECTORS]}"
    )


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


# ---------------------------------------------------------------------------
# Emisores del alta masiva IAMC (resueltos contra el Universo BYMA, 2026-08-31)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("emisor,esperado", [
    # Sectores NUEVOS
    ("LABORATORIOS RICHMOND S.A.C.I.F.", "Salud / Farma"),
    ("Minera Exar S.A.",                 "Minería"),
    # Generadoras renovables -> Energía (Utilities es para DISTRIBUIDORAS).
    # 360 Energy y Luz de Tres Picos tienen que caer igual que Genneia.
    ("360 Energy Solar S.A.",            "Energía / Petróleo & Gas"),
    ("Luz de Tres Picos S.A.",           "Energía / Petróleo & Gas"),
    ("GENNEIA S.A.",                     "Energía / Petróleo & Gas"),
    ("Crown Point Energía S.A.",         "Energía / Petróleo & Gas"),
    ("Petróleos Sudamericanos S.A.",     "Energía / Petróleo & Gas"),
    # Agro
    ("Rizobacter Argentina S.A.",        "Agro / Alimentos"),
    ("PROFERTIL S.A.",                   "Agro / Alimentos"),
    ("Havanna S.A.",                     "Agro / Alimentos"),
    ("INVERSORA JURAMENTO S.A.",         "Agro / Alimentos"),
    ("Futuros y Opciones.com S.A.",      "Agro / Alimentos"),
    ("Ricardo Venturino S.A.",           "Agro / Alimentos"),
    ("Mastellone Hermanos S.A.",         "Agro / Alimentos"),
    # Financieras CAUTIVAS de fabricantes de maquinaria: el emisor de la ON es la
    # financiera (riesgo de crédito prendario), no la fábrica → Servicios Financieros,
    # igual que Scania Credit. Nombres legales tal cual los trae el Universo BYMA
    # (data/byma/titulos_final.csv): con esos nombres "JOHN DEERE" en Industrial era
    # inalcanzable, así que el bucket queda FIJADO acá.
    ("John Deere Credit Cía. Financiera S.A.", "Servicios Financieros"),
    ("CNH Industrial Capital Argentina S.A.",  "Servicios Financieros"),
    # Otros sectores
    ("Grupo ST S.A.",                    "Servicios Financieros"),
    ("Plaza Logística S.R.L.",           "Real Estate"),
    ("Empresa Distribuidora y Comercializadora Norte S.A.", "Utilities (Luz / Gas)"),
])
def test_emisores_iamc_caen_en_su_sector(emisor, esperado):
    assert classify_sector(emisor) == esperado


def test_los_sectores_nuevos_tienen_metadata_completa():
    """Un sector sin color/icono rompe el pintado del panel."""
    for key in ("Salud / Farma", "Minería"):
        m = sector_meta(key)
        assert m.key == key, f"{key} no esta en SECTORS"
        assert m.color.startswith("#") and len(m.color) == 7
        assert m.icon and m.short
