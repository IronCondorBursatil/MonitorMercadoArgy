"""Dominio FCI — lógica pura para armar el dataset del panel (subcategorías,
unificación de clases, AUM join, histórico de cuotaparte, lente de moneda, flujos).

Esta es la fuente de verdad runtime que reemplaza al viejo `scripts/build_fci_mock_data.py`
(borrado): a partir del parse enriquecido de CAFCI (`cafci_provider._parse_payload`) + AUM de
ArgentinaDatos + macro real (A3500/CER vía BCRA) + flujos reales (`fci_history`, Δccp×VCP) arma
la forma que consume el front (`apps/web/static/js/fci.js`).

Convenciones:
  - Composición de cartera: NO se sintetiza (sin fuente pública real; CAFCI ficha gateada).
  - Flujos: solo reales (de `fci_history`); si no hay historia acumulada, el front muestra
    "acumulando…". `meta.flows_real` indica si hay al menos un flujo real.
  - Histórico de cuotaparte: reconstruido de los retornos reales por período (anclado), y
    reemplazado por la serie real de `fci_history` cuando cubre la ventana (`hist_real`).
"""

# Períodos de la matriz CAFCI (sin punto a 1 día; el más corto es 7d).
PERIODS = ("dias_7", "mes_1", "dias_90", "dias_180", "ytd", "meses_12")
PERIOD_LABELS = {"dias_7": "7D", "mes_1": "1M", "dias_90": "3M",
                 "dias_180": "6M", "ytd": "YTD", "meses_12": "12M"}
# Días por período (para anualizar/deflactar; ytd se calcula aparte del fecha_base).
PERIOD_DAYS = {"dias_7": 7, "mes_1": 30, "dias_90": 90, "dias_180": 180, "meses_12": 365}
# Anclas del histórico reconstruido (ytd excluido: arranque irregular).
ANCHORS = [("dias_7", 7), ("mes_1", 30), ("dias_90", 90), ("dias_180", 180), ("meses_12", 365)]
# Puntos del histórico de VCP (~cada 3 días sobre ~12m) para que los rangos cortos se vean bien.
HIST_N = 120
# Orden de categorías (buckets) para la vista Mercado.
CAT_ORDER = ["Mercado de Dinero", "Renta Fija", "Renta Variable", "Renta Mixta",
             "Retorno Total", "Otros"]

# Endpoints FCI de ArgentinaDatos (categoría → URL del último corte). Fuente única,
# compartida por el acumulador (fci_history) y el fallback del CAFCIProvider.
_ARD = "https://api.argentinadatos.com/v1/finanzas/fci"
ARD_FCI_ENDPOINTS = {
    "Mercado de Dinero": f"{_ARD}/mercadoDinero/ultimo",
    "Renta Variable":    f"{_ARD}/rentaVariable/ultimo",
    "Renta Fija":        f"{_ARD}/rentaFija/ultimo",
    "Renta Mixta":       f"{_ARD}/rentaMixta/ultimo",
    "Retorno Total":     f"{_ARD}/retornoTotal/ultimo",
}
