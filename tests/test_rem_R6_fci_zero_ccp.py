"""Remediación lote R6 (FCI) — `ccp = 0` de ArgentinaDatos es DATO AUSENTE, no una
circulación real de cero.

`net_flow_series` tomaba las filas con `ccp = 0` como observación válida y fabricaba dos
flujos fantasma simétricos, ninguno de los cuales atrapaba el guard `_NET_FLOW_MAX_JUMP`
(que exige `ccp_prev > 0` para el primero y da ratio 1,0 —plausible— para el segundo):

  · `0 → X`  suscripción por el patrimonio ENTERO del fondo;
  · `X → 0`  rescate total del fondo.

Medido sobre el store real del usuario (`%LOCALAPPDATA%/monitor/fci_history.db`, cortes
2026-06-09/10 y 2026-08-30/31, 16.830 filas): **2.122 de las 4.706 filas de cada corte
traen ccp = 0** (45%), y de ahí salen **40 transiciones 0→positivo (+1,0142e11)** y **37
positivo→0 (−8,2657e10)**. Ejemplos textuales del store, usados como fixtures abajo:

  · 'toronto trust special opportunities - clase b': ccp 0 · 0 · 10.332.809 · 10.332.809
    → publicaba +3,943e10 de suscripción el 30/08 (la clase no cambió de tamaño: el 09 y
    el 10 de junio simplemente no traían ccp).
  · 'fima premium - clase p': ccp 380.761.883 · 382.692.286 · 0 · 0 → publicaba −2,899e10
    de rescate el 30/08 y se comía el flujo REAL de +1,46e8 del 10/06.

La distinción "alta real vs hueco de datos": el alta real de un fondo NO se pierde, porque
el primer punto con `ccp` de una serie nunca genera flujo (no hay previo contra el cual
medirlo). Lo que se descarta es la reaparición del dato dentro de una serie ya empezada.
"""

from datetime import date

import pytest

from core.infrastructure.fci_history import net_flow_series

_D = [date(2026, 6, 9), date(2026, 6, 10), date(2026, 8, 30), date(2026, 8, 31)]


def _serie(rows):
    return {d: {"vcp": v, "ccp": c, "patrimonio": p} for d, (v, c, p) in zip(_D, rows)}


# --- filas TEXTUALES del store real ------------------------------------------------
_TORONTO = _serie([                       # 'toronto trust special opportunities - clase b'
    (230884.041, 0.0, 0.0),
    (230884.041, 0.0, 0.0),
    (3815892.834, 10_332_809.06, 39_428_892_036.81),
    (3815892.834, 10_332_809.06, 39_428_892_036.81),
])
_FIMA_P = _serie([                        # 'fima premium - clase p'
    (75515.301, 380_761_883.0, 28_753_348_320.0),
    (75615.832, 382_692_286.0, 28_937_595_755.0),
    (76123.386, 0.0, 0.0),
    (76123.386, 0.0, 0.0),
])


def test_ccp_cero_a_positivo_no_es_una_suscripcion():
    """El +3,943e10 fantasma de 'toronto trust special opportunities - clase b'."""
    flows = net_flow_series(_TORONTO)
    assert date(2026, 8, 30) not in flows, "suscripción inventada por el hueco de datos"
    assert flows.get(date(2026, 8, 31), 0.0) == pytest.approx(0.0)
    assert sum(flows.values()) == pytest.approx(0.0)


def test_positivo_a_ccp_cero_no_es_un_rescate_total():
    """El −2,899e10 fantasma de 'fima premium - clase p' — y el flujo REAL del 10/06,
    que quedaba sepultado bajo el fantasma, se conserva."""
    flows = net_flow_series(_FIMA_P)
    assert date(2026, 8, 30) not in flows and date(2026, 8, 31) not in flows
    real = (382_692_286.0 - 380_761_883.0) * (28_937_595_755.0 / 382_692_286.0)
    assert flows[date(2026, 6, 10)] == pytest.approx(real, rel=1e-9)
    assert sum(flows.values()) == pytest.approx(real, rel=1e-9)


def test_la_serie_se_puentea_sobre_el_hueco():
    """Un hueco intermedio no parte la serie: el flujo entre los dos días con dato se
    imputa al segundo (igual que con los días sin publicación)."""
    s = _serie([(100.0, 1_000.0, 100_000.0), (100.0, 0.0, 0.0),
                (100.0, 1_200.0, 120_000.0), (100.0, 1_200.0, 120_000.0)])
    flows = net_flow_series(s)
    assert set(flows) == {date(2026, 8, 30), date(2026, 8, 31)}
    assert flows[date(2026, 8, 30)] == pytest.approx(200 * 100.0)   # +200 cuotapartes
    assert flows[date(2026, 8, 31)] == pytest.approx(0.0)


def test_el_alta_real_de_un_fondo_no_genera_flujo_fantasma():
    """Un fondo que entra al store ya con circulación: su primer punto no tiene previo,
    así que nunca hubo (ni hay) flujo por el patrimonio entero. Los flujos ORGÁNICOS
    siguientes sí se miden."""
    s = {date(2026, 8, 30): {"vcp": 1000.0, "ccp": 5_000.0, "patrimonio": 5_000_000.0},
         date(2026, 8, 31): {"vcp": 1000.0, "ccp": 5_500.0, "patrimonio": 5_500_000.0}}
    flows = net_flow_series(s)
    assert date(2026, 8, 30) not in flows
    assert flows[date(2026, 8, 31)] == pytest.approx(500 * 1000.0)


def test_ccp_negativo_o_None_tambien_es_dato_ausente():
    """`record_snapshot` solo exige `ccp is not None`; un 0/negativo de la fuente no puede
    entrar como observación."""
    s = {date(2026, 8, 30): {"vcp": 100.0, "ccp": -1.0, "patrimonio": 0.0},
         date(2026, 8, 31): {"vcp": 100.0, "ccp": 10.0, "patrimonio": 1.0}}
    assert net_flow_series(s) == {}
    s2 = {date(2026, 8, 30): {"vcp": 100.0, "ccp": None, "patrimonio": None},
          date(2026, 8, 31): {"vcp": 100.0, "ccp": 10.0, "patrimonio": 1.0}}
    assert net_flow_series(s2) == {}


def test_no_divide_por_cero_con_ccp_cero_en_todos_los_puntos():
    s = _serie([(100.0, 0.0, 0.0)] * 4)
    assert net_flow_series(s) == {}          # no explota ni inventa flujo
