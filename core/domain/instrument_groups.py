"""Canonical groupings of instrument_type values used by every monitor.

Single source of truth: changing a curve's universe means editing this file,
never a monitor or the web schema. Values must match the `instrument_type`
column in `data/instruments_master.xlsx` (uppercased, stripped).
"""

SOBERANOS = ["BONAR", "GLOBAL"]
BOPREALES = ["BOPREAL"]
TASA_FIJA = ["LECAP", "BONCAP", "BONOFIJA"]
CER = ["CER", "LECER", "BONCER", "BONCER ZC", "CON CUPON", "STEP-UP"]
DOLAR_LINKED = ["DOLAR_LINKED"]
TAMAR = ["PURO"]            # TAMAR-linked: pay accrued TAMAR rate at maturity
DUAL_TAMAR = ["DUAL", "DUAL_CER_TAMAR"]  # Dual TAMAR (fixed-floor) + Dual CER/TAMAR (new TXMJ* series)
OBLIGACIONES_NEGOCIABLES = ["HARD DOLLAR", "DOLLAR LINKED"]  # ONs corporativas: hard-dollar (paga USD) / dollar-linked (paga pesos × FX). Ambos bajo categoría "Obligaciones Negociables".

# Deuda SUBSOBERANA (provincias y municipios). Tipos propios —NO se reusan los de las
# ONs corporativas— para que tengan su propio panel: los paneles filtran por igualdad
# exacta de `instrument_type`, mientras que los predicados del motor (`is_hard_dollar`,
# `is_cer`, `is_dolar_linked`) matchean por SUBSTRING. Resultado: "PROVINCIAL HARD DOLLAR"
# se precia con HardDollarStrategy (idéntico a una ON hard-dollar: pata pesos → USD por
# MEP si es ley AR / CCL si es ley EXT) pero NO aparece en el panel de ONs corporativas.
PROVINCIAL_USD = ["PROVINCIAL HARD DOLLAR"]
# "PROVINCIAL ARS" = cupón periódico en pesos (TAMAR/BADLAR/dual) sobre cashflow
# explícito. NO matchea ningún predicado del motor → se precia con VanillaStrategy,
# exactamente igual que como venían tipados (BONOFIJA); solo cambia de panel.
PROVINCIAL_ARS = ["PROVINCIAL ARS", "PROVINCIAL CER", "PROVINCIAL DOLAR_LINKED"]
PROVINCIALES = PROVINCIAL_USD + PROVINCIAL_ARS

# BYMA Panel Líder — universo de acciones que componen el índice principal
# del mercado argentino (revisado trimestralmente por BYMA). No son
# instrumentos del Excel; sólo se cotizan vía Data912 /arg_stocks.
PANEL_LIDER = [
    "ALUA", "BBAR", "BMA", "BYMA", "CEPU", "COME", "CRES", "EDN", "GGAL",
    "LOMA", "METR", "PAMP", "SUPV", "TECO2", "TGNO4", "TGSU2", "TRAN",
    "TXAR", "VALO", "YPFD",
]


# --------------------------------------------------------------------------- #
# Universo de `instrument_type` VÁLIDOS.
#
# Todo el read-path (paneles, `apps/web/app.py::_ALL_TYPES`, `on_service`) filtra
# por IGUALDAD EXACTA de `instrument_type` contra las listas de arriba: un tipo que
# no figure en ninguna NO se precia y NO se ve en ningún panel — el bono queda
# cargado pero invisible. Por eso el borde de escritura (`build_instrument` /
# ABM) valida contra este set en vez de inventar un tipo del nombre de la hoja.
#
# `ACCION` no tiene panel por tipo (el Panel Líder cotiza por ticker) pero es un
# tipo legítimo del catálogo (altas de `instruments_abm.register_stocks`).
# --------------------------------------------------------------------------- #
ACCIONES = ["ACCION"]

# --------------------------------------------------------------------------- #
# Tipos de PAYOFF ANALÍTICO (fórmula cerrada). Su pago a vencimiento NO sale de un
# schedule materializado sino de `core/domain/pricing/tamar.tamar_dual_payoff_at`
# sobre la TAMAR observada+proyectada (y, en DUAL_CER_TAMAR, el max contra el riel
# CER). El registry los rutea a TamarStrategy / DualCerTamarStrategy, que no leen
# `inst.cashflows` en su camino principal.
#
# Consecuencia operativa: persistirles un schedule nominal sería *incorrecto*, no
# una optimización. En la DB llevan una sola fila ANCLA (`CashflowORM.es_ancla`)
# con el vencimiento y monto 0, que `catalog_repository._orm_to_domain` filtra.
#
# La lista está verificada CONTRA el registry (no escrita de memoria):
# `tests/test_perf_W1_cashflows_ancla.py::test_analytic_payoff_types_coincide_con_el_registry`
# recorre BOND_TYPES y exige la equivalencia exacta con `strategy_for`. Si mañana
# una familia nueva estrena payoff cerrado, ese test rompe hasta agregarla acá.
ANALYTIC_PAYOFF_TYPES = frozenset({*TAMAR, *DUAL_TAMAR})   # PURO, DUAL, DUAL_CER_TAMAR


def has_closed_form_payoff(instrument_type) -> bool:
    """True si el payoff del tipo es de fórmula cerrada (ver ANALYTIC_PAYOFF_TYPES).
    Normaliza igual que `is_known_type` (upper + strip, None/'' → False)."""
    return str(instrument_type or "").upper().strip() in ANALYTIC_PAYOFF_TYPES

BOND_TYPES = [*SOBERANOS, *BOPREALES, *TASA_FIJA, *CER, *DOLAR_LINKED, *TAMAR,
              *DUAL_TAMAR, *OBLIGACIONES_NEGOCIABLES, *PROVINCIALES]

KNOWN_TYPES = frozenset(BOND_TYPES + ACCIONES)


def is_known_type(instrument_type) -> bool:
    """True si `instrument_type` pertenece a algún grupo (= algún panel lo muestra)."""
    return str(instrument_type or "").upper().strip() in KNOWN_TYPES


def orphan_types(types) -> list:
    """Los tipos de `types` que no pertenecen a ningún grupo, ordenados y sin
    repetir. Vacío = todo el universo es visible en algún panel."""
    return sorted({str(t or "").upper().strip() for t in types
                   if not is_known_type(t)})
