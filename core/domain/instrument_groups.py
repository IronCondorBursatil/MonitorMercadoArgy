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

# BYMA Panel Líder — universo de acciones que componen el índice principal
# del mercado argentino (revisado trimestralmente por BYMA). No son
# instrumentos del Excel; sólo se cotizan vía Data912 /arg_stocks.
PANEL_LIDER = [
    "ALUA", "BBAR", "BMA", "BYMA", "CEPU", "COME", "CRES", "EDN", "GGAL",
    "LOMA", "METR", "PAMP", "SUPV", "TECO2", "TGNO4", "TGSU2", "TRAN",
    "TXAR", "VALO", "YPFD",
]
