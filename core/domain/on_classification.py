"""Clasificación de Obligaciones Negociables (ON) por SECTOR.

Criterio: match por keywords sobre el NOMBRE del emisor (no existe un campo sector
en el catálogo/CSV/ORM — solo el emisor). Es determinístico, sin I/O y testeable;
se calcula al vuelo (no se persiste) en el endpoint `/on` (y se sirve en
`/on/data::sectors_meta` para que el cliente pinte igual que el server). Módulo de
dominio puro, estilo `conventions.py`.

Keywords ANCLADAS AL NOMBRE de la sociedad (no tokens genéricos como "ENERGIA" que
contaminan). El ORDEN importa: primer match gana — Utilities (distribuidoras) antes
que Energía (generadoras/petróleo) para que EDENOR/EDEMSA caigan en Utilities y
Central Puerto/MSU/Genneia en Energía.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

OTROS = "Otros"


@dataclass(frozen=True)
class SectorMeta:
    key: str     # etiqueta canónica (= la clave de sector que usan los datasets)
    short: str   # nombre corto para UI
    color: str   # color de marca (hex)
    icon: str    # emoji


# Orden canónico + paleta + íconos (espejo de apps/web/on_src/sectors.js).
# El front (static/js/on.js) hidrata desde /on/data (sectors_meta); la copia JS de
# on_src/sectors.js es solo fallback de arranque. Esta es la fuente Python de verdad.
SECTORS: list[SectorMeta] = [
    SectorMeta("Energía / Petróleo & Gas", "Energía", "#ED8B36", "⚡"),
    SectorMeta("Utilities (Luz / Gas)", "Utilities", "#2FA4C9", "🔌"),
    SectorMeta("Servicios Financieros", "Serv. Financieros", "#6C5CE0", "🏦"),
    SectorMeta("Agro / Alimentos", "Agro", "#2FB36B", "🌾"),
    SectorMeta("Industrial / Maquinaria", "Industrial", "#C0573C", "⚙️"),
    SectorMeta("Infraestructura / Construcción", "Infra", "#C9A227", "🏗️"),
    SectorMeta("Real Estate", "Real Estate", "#D85FA0", "🏢"),
    SectorMeta("Telecomunicaciones", "Telco", "#0E9C8A", "📡"),
    SectorMeta("Salud / Farma", "Salud", "#E0566E", "💊"),
    SectorMeta("Minería", "Minería", "#8D6E63", "⛏️"),
    SectorMeta(OTROS, "Otros", "#8993B8", "•"),
]
SECTOR_MAP: dict[str, SectorMeta] = {s.key: s for s in SECTORS}

# etiqueta del sector -> keywords (en MAYÚSCULA, sin acentos). Orden = prioridad.
SECTOR_KEYWORDS: list[tuple[str, list[str]]] = [
    ("Servicios Financieros", ["BANCO", "GALICIA", "MACRO", "COMAFI", "SUPERVIELLE",
                               "HIPOTECARIO", "BBVA", "SANTANDER", "TARJETA NARANJA",
                               "MERCADO PAGO", "SCANIA", "CREDITO", "FINANCIERA",
                               "COMPANIA FINANCIERA", "TARJETA", "GRUPO ST"]),
    ("Utilities (Luz / Gas)", ["EDENOR", "EDESUR", "EDEMSA", "EDELAP", "EDESA",
                               "TRANSENER", "METROGAS", "CAMUZZI", "NATURGY",
                               # razones sociales del Universo BYMA (no traen la sigla)
                               "DISTRIBUIDORA Y COMERCIALIZADORA NORTE",
                               "DISTRIBUIDORA DE ELECTRICIDAD DE MENDOZA"]),
    ("Agro / Alimentos", ["CRESUD", "MASTELLONE", "LEDESMA", "MOLINOS", "ARCOR",
                          "AGROFINA", "BIOCERES", "SAN MIGUEL", "ADECOAGRO",
                          # alta IAMC 2026-08: agroinsumos, ganaderia, alimentos y
                          # el broker de granos FyO (no es una financiera).
                          "RIZOBACTER", "PROFERTIL", "HAVANNA", "JURAMENTO",
                          "FUTUROS Y OPCIONES", "VENTURINO"]),
    ("Industrial / Maquinaria", ["JOHN DEERE", "CNH", "MIRGOR", "ALUAR", "LOMA NEGRA",
                                 "SIDERAR", "TERNIUM", "FCA", "TOYOTA", "VOLKSWAGEN",
                                 "GENERAL MOTORS"]),
    ("Infraestructura / Construcción", ["AEROPUERTOS", "CLISA", "CAPUTO", "CRIBA",
                                        "ROVELLA", "AUTOPISTAS"]),
    ("Real Estate", ["IRSA", "RAGHSA", "TGLT", "PLAZA LOGISTICA"]),
    ("Telecomunicaciones", ["TELECOM"]),
    ("Salud / Farma", ["RICHMOND", "LABORATORIOS"]),
    ("Minería", ["MINERA EXAR", "LITIO"]),
    ("Energía / Petróleo & Gas", ["PAMPA", "PECOM", "CAPEX", "COMPANIA MEGA", "MEGA",
                                  "MSU", "GENNEIA", "CENTRAL PUERTO", "COMBUSTIBLES",
                                  "CGC", "OLEODUCTOS", "OILTANKING", "EBYTEM", "YPF",
                                  "VISTA", "TECPETROL", "PLUSPETROL", "PAN AMERICAN",
                                  "TGS", "TGN", "ALBANESI", "RAGHSA OIL",
                                  "COMODORO RIVADAVIA",
                                  # alta IAMC 2026-08. 360 Energy y Luz de Tres Picos
                                  # son GENERADORAS renovables -> Energia, igual que
                                  # Genneia; Utilities queda para DISTRIBUIDORAS.
                                  "360 ENERGY", "TRES PICOS", "CROWN POINT",
                                  "PETROLEOS SUDAMERICANOS",
                                  # nombres legales BYMA (el emisor refrescado no trae la sigla)
                                  "TRANSPORTADORA DE GAS DEL SUR",
                                  "TRANSPORTADORA DE GAS DEL NORTE"]),
]


def normalize_issuer(s: str) -> str:
    """NFKD + saca diacríticos + upper + strip (para el match por keyword)."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.upper().strip()


def classify_sector(emisor: str) -> str:
    """Sector (key canónica) del emisor; primer match gana, fallback 'Otros'."""
    n = normalize_issuer(emisor)
    for label, kws in SECTOR_KEYWORDS:
        if any(k in n for k in kws):
            return label
    return OTROS


def sector_meta(key: str) -> SectorMeta:
    """Metadata del sector (color/icon/short); 'Otros' si la key no existe."""
    return SECTOR_MAP.get(key, SECTOR_MAP[OTROS])


def sector_for(inst) -> str:
    """Sector (key canónica) de un instrumento: usa el override manual elegido en el
    ABM (`inst.sector_override`, si es una key válida) y si no lo deduce del emisor.
    Así el operador puede re-ordenar una ON sin depender del match por nombre."""
    ov = getattr(inst, "sector_override", None)
    if ov and ov in SECTOR_MAP:
        return ov
    return classify_sector(getattr(inst, "short_name", "") or "")
