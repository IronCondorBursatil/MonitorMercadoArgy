"""Calendario BYMA: día hábil con prioridad feriados oficiales AR (Excel/API)
y fallback al paquete pandas (XBUE) para años sin cobertura.

Regresión del bug donde XBUE (pandas_market_calendars) tenía mal los feriados
argentinos: faltaban puentes turísticos y trasladables (2026-03-23, 2025-11-21,
2025-11-24) y agregaba fechas espurias (2025-11-17), lo que hacía que
`cer_reference_date` aterrizara 1-2 días hábiles tarde. Los cer_base del master
(validados contra BCRA var 30) corresponden a -10 hábiles con el calendario AR
COMPLETO; este test fija ese calendario.
"""

from datetime import date

from core.holiday_engine import is_habil, settlement_byma
from core.domain.conventions import cer_reference_date


def test_feriados_reales_no_son_habiles():
    # Puentes turísticos + trasladables oficiales (argentinadatos) — mercado cerrado.
    assert is_habil("2026-03-23") is False   # puente turístico
    assert is_habil("2026-03-24") is False   # Día de la Memoria (inamovible)
    assert is_habil("2025-11-21") is False   # puente turístico
    assert is_habil("2025-11-24") is False   # Día de la Soberanía (trasladable)


def test_fecha_espuria_de_xbue_si_es_habil():
    # 2025-11-17 venía SOLO de xbue_pmc (no es feriado real) → debe ser hábil.
    assert is_habil("2025-11-17") is True


def test_dias_normales_y_finde():
    assert is_habil("2026-03-25") is True    # miércoles laborable
    assert is_habil("2026-03-21") is False   # sábado
    assert is_habil("2026-03-22") is False   # domingo


def test_cer_reference_date_cruza_puentes():
    # -10 hábiles desde la emisión, con el calendario AR completo.
    # TZXS7/TZXS8/TZXM8 (cer_base 723.06 = CER 2026-03-13).
    assert cer_reference_date(date(2026, 3, 31), 10) == date(2026, 3, 13)
    # X29Y6/TZXA7 (cer_base 651.89806 = CER 2025-11-12).
    assert cer_reference_date(date(2025, 11, 28), 10) == date(2025, 11, 12)


def test_settlement_t1_salta_feriado():
    # Trade el viernes 2026-03-20: T+1 debe saltar finde + puente 23 + memoria 24
    # y caer el miércoles 2026-03-25.
    s = settlement_byma("2026-03-20", lag=1)
    assert s.date() == date(2026, 3, 25)


# --------------------------------------------------------------------------- #
# Hash del calendario (Fase 5 de agents.md §0.8). TODO el settlement y el lag CER cuelgan
# de data/feriados_ar.xlsx: una edición silenciosa del Excel (o un `py core/holiday_engine.py`
# sin argumentos, que lo REESCRIBE desde las APIs) mueve el V.Téc de cada bono indexado sin
# que ningún spot-check lo note. Se hashea el conjunto DERIVADO restringido a la cobertura
# (2020–2029), no los bytes del xlsx (un re-save de Excel cambia metadata) ni años fuera de
# cobertura (dependen de la versión de pandas_market_calendars). Si este test se pone rojo
# por un cambio LEGÍTIMO del calendario, se actualiza el hash EN EL MISMO COMMIT, con el
# motivo (feriado nuevo/trasladado y su fuente oficial).
# --------------------------------------------------------------------------- #
_HASH_FERIADOS_2020_2029 = "56aed962fb13dc47164060044ce7a1dab7deede6f02a0e1c959c12a3d5ad2818"


def _hash_calendario(fechas) -> str:
    import hashlib
    return hashlib.sha256("\n".join(sorted(d.isoformat() for d in fechas)).encode()).hexdigest()


def test_calendario_ar_2020_2029_no_cambio_en_silencio():
    from core import holiday_engine as he

    fer = he._ar_holidays()
    assert he._ar_cobertura() == (2020, 2029), "cambió la cobertura del Excel de feriados"
    en_cobertura = {d for d in fer if 2020 <= d.year <= 2029}
    assert len(en_cobertura) == 197, f"{len(en_cobertura)} feriados en 2020-2029 (esperaba 197)"
    assert _hash_calendario(en_cobertura) == _HASH_FERIADOS_2020_2029, (
        "el conjunto de feriados 2020-2029 cambió: si es legítimo, actualizá el hash con el "
        "motivo y la fuente oficial en el mismo commit")


def test_el_hash_del_calendario_detecta_un_feriado_de_mas_o_de_menos():
    """Prueba por mutación del guard de arriba (sin tocar el Excel): un feriado agregado o
    sacado cambia el hash."""
    from core import holiday_engine as he

    base = {d for d in he._ar_holidays() if 2020 <= d.year <= 2029}
    assert _hash_calendario(base) == _HASH_FERIADOS_2020_2029
    assert _hash_calendario(base | {date(2027, 7, 15)}) != _HASH_FERIADOS_2020_2029
    assert _hash_calendario(base - {date(2026, 3, 24)}) != _HASH_FERIADOS_2020_2029
