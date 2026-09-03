"""Auditoría lote E — `net_flow_series` valúa las cuotapartes al precio unitario REAL.

ArgentinaDatos publica el `vcp` **por cada 1.000 cuotapartes**, no por una. Verificado
contra el store real (`%LOCALAPPDATA%/monitor/fci_history.db`, cortes 2026-06-09..
2026-08-31, 16.830 filas): de las 9.394 filas con vcp/ccp/patrimonio no nulos,
**9.276 cumplen `patrimonio == ccp·vcp/1000` con error < 1e-6** (p01 0.9999813 /
p50 1.0 / p99 1.0002354 del cociente `(pat/ccp)/(vcp/1000)`); los 114 restantes son
redondeo a entero de ccp/patrimonio en fondos diminutos (0,0025% del patrimonio total
del corte). Solo 2 fondos ("investire … valor de liq. final de cp") publican el valor
por 1 cuotaparte (`patrimonio == ccp·vcp`), de ahí que el precio unitario se derive por
fila de `patrimonio/ccp` y no de una constante global.

Las filas de este test son COPIAS TEXTUALES del store real (ver docstrings), para que el
test sea hermético (no lee la DB del usuario) pero siga siendo evidencia de campo.
"""

from datetime import date

import pytest

from core.infrastructure.fci_history import net_flow_series

# --- filas reales del store (fondo 'super ahorro $ - clase b') ------------------
_SUPER_AHORRO = {
    date(2026, 6, 9): {"vcp": 23065.77, "ccp": 103_980_682_477.0, "patrimonio": 2_398_394_518_245.0},
    date(2026, 6, 10): {"vcp": 23076.728, "ccp": 103_479_659_679.0, "patrimonio": 2_387_972_006_197.0},
}


def test_net_flow_uses_real_unit_price_not_vcp_per_1000():
    """El flujo del 10/06 de 'super ahorro $ - clase b' con datos reales del store.

    Δccp = −501.022.798 cuotapartes; precio unitario = patrimonio/ccp = 23,0767 $/cp
    (= vcp/1000). Flujo real ≈ −$11.562 M. Con `Δccp × vcp` crudo daba −1,156e13,
    o sea 1.000× el importe: un rescate de casi 5× el patrimonio del fondo en un día.
    """
    flows = net_flow_series(_SUPER_AHORRO)
    d = date(2026, 6, 10)
    row = _SUPER_AHORRO[d]
    delta_ccp = row["ccp"] - _SUPER_AHORRO[date(2026, 6, 9)]["ccp"]
    expected = delta_ccp * (row["patrimonio"] / row["ccp"])       # ≈ −1,1562e10

    assert flows[d] == pytest.approx(expected, rel=1e-9)
    # y coincide (0,1%) con la convención publicada vcp/1000
    assert flows[d] == pytest.approx(delta_ccp * row["vcp"] / 1000.0, rel=1e-3)
    # sanity de escala: un rescate diario no puede superar el patrimonio del fondo
    assert abs(flows[d]) < row["patrimonio"]


def test_net_flow_respects_per_share_funds():
    """Los 2 fondos 'investire … (valor de liq. final de cp)' publican `patrimonio ==
    ccp·vcp` (VCP por 1 cuotaparte). Derivando el precio de patrimonio/ccp salen bien
    sin caso especial; dividir por 1000 a ciegas los dejaría 1.000× subvaluados."""
    # vcp/ccp/patrimonio del 2026-08-31 (reales); el ccp del día siguiente es sintético
    # (el store todavía no tiene un Δ para este fondo) para poder medir un flujo.
    series = {
        date(2026, 8, 30): {"vcp": 3316.185, "ccp": 447.2483591838212, "patrimonio": 1_483_158.3},
        date(2026, 8, 31): {"vcp": 3316.185, "ccp": 547.2483591838212,
                            "patrimonio": 547.2483591838212 * 3316.185},
    }
    flows = net_flow_series(series)
    assert flows[date(2026, 8, 31)] == pytest.approx(100.0 * 3316.185, rel=1e-9)


def test_net_flow_falls_back_to_vcp_per_1000_without_patrimonio():
    """7.410 de las 16.830 filas del store traen `patrimonio` 0/NULL y 7.434 traen
    `ccp` 0 (record_snapshot solo exige `fondo` y `ccp is not None`). Sin fallback,
    `patrimonio/ccp` dividiría por cero; el fallback es la convención publicada."""
    series = {
        date(2026, 6, 2): {"vcp": 1000.0, "ccp": 5_000.0, "patrimonio": None},
        date(2026, 6, 3): {"vcp": 1100.0, "ccp": 6_000.0, "patrimonio": 0.0},
    }
    flows = net_flow_series(series)
    assert flows[date(2026, 6, 3)] == pytest.approx(1000.0 * 1100.0 / 1000.0)


def test_net_flow_does_not_divide_by_zero_ccp():
    """`ccp == 0` entra al store (es `not None`) y el precio unitario no puede explotar.

    Además —remediación R6— ese punto NO es una observación: `ccp = 0` es "ArgentinaDatos
    no publicó el dato" (2.122 de 4.706 filas del corte real), así que la transición
    0→positivo no puede leerse como una suscripción por el patrimonio entero. Ver
    tests/test_rem_R6_fci_zero_ccp.py para la evidencia de campo."""
    series = {
        date(2026, 6, 2): {"vcp": 100.0, "ccp": 0.0, "patrimonio": 0.0},
        date(2026, 6, 3): {"vcp": 100.0, "ccp": 10.0, "patrimonio": 1.0},
    }
    flows = net_flow_series(series)          # no debe romper
    assert flows == {}
