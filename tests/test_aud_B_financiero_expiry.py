"""Auditoría lote B — `resolve_expiry_date` no debe rolar un candidato futuro.

El margen de 5 días mandaba la cohorte del mes EN CURSO al tercer viernes del
AÑO SIGUIENTE durante toda su propia semana de vencimiento (5 ruedas por mes).
"""
from __future__ import annotations

from datetime import date, timedelta

from core.domain.options.expiry import resolve_expiry_date, third_friday


def test_expiry_semana_de_vencimiento_no_rola_al_anio_siguiente():
    """Sep-2026: el 3er viernes es el 18. Del domingo 13 al viernes 18 la serie
    de septiembre sigue viva → debe resolver a 2026-09-18, no a 2027."""
    tf = third_friday(2026, 9)
    assert tf == date(2026, 9, 18)
    for d in range(13, 19):
        today = date(2026, 9, d)
        assert resolve_expiry_date(9, today=today) == tf, f"today={today}"


def test_expiry_el_mismo_dia_del_vencimiento_sigue_siendo_valido():
    assert resolve_expiry_date(9, today=date(2026, 9, 18)) == date(2026, 9, 18)


def test_expiry_rola_solo_cuando_ya_paso():
    """Sábado 19-Sep-2026: el vto de septiembre ya pasó → año siguiente."""
    assert resolve_expiry_date(9, today=date(2026, 9, 19)) == third_friday(2027, 9)


def test_expiry_mes_futuro_del_anio_en_curso_no_se_toca():
    assert resolve_expiry_date(12, today=date(2026, 9, 3)) == third_friday(2026, 12)


def test_expiry_nunca_esta_en_el_pasado():
    """Barrido: para cualquier día del 2026 y cualquier mes, el vto resuelto
    nunca queda antes de hoy ni más de 1 año + 1 mes adelante."""
    d = date(2026, 1, 1)
    while d < date(2027, 1, 1):
        for m in range(1, 13):
            e = resolve_expiry_date(m, today=d)
            assert e >= d, (d, m, e)
            assert (e - d).days <= 400, (d, m, e)
        d += timedelta(days=1)
