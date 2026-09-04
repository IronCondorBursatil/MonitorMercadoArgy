"""W1 (dominio) — Fase 3, item 1: memoización de `parse_day_count`.

`Instrument.day_count_enum` llama a `parse_day_count` POR CASHFLOW en cada
`year_fraction_to` (models.py) → ~13.300 parseos de string por ciclo de pricing
sobre un dominio de 4-5 grafías. La función es PURA (string → enum, sin estado,
sin I/O, sin reloj) ⇒ memoizable sin mover un solo número.

Estos tests cubren:
  1. que el memo EXISTE y pega (mutación: sin `lru_cache` no hay `cache_info`),
  2. que NO cambia el resultado para ninguna grafía (oráculo de pureza contra la
     implementación sin cachear),
  3. que sigue sin lanzar ante un argumento NO hasheable (contrato "NUNCA lanza":
     `lru_cache` pelado lo rompería con TypeError),
  4. que la grafía real de TODO el catálogo se parsea igual con memo limpio y
     con memo poblado.
"""

from __future__ import annotations

import pytest

from core.domain.daycount import DayCount, parse_day_count

_ALIASES = [
    "ACT/365", "act/365", "  ACT/365  ", "ACTUAL/365", "Actual/365",
    "ACT/365.25", "act/365.25", "ACT/365,25", "ACTUAL/365.25",
    "30/360", "30E/360", "30/360 US",
    "ACT/ACT", "act/act", "ACTUAL/ACTUAL", "ACT/ACT ISDA",
    "", "   ", None, "garbage", "xyz", "???", "366",
]


def test_parse_day_count_is_memoized():
    """El memo existe y PEGA: dos llamadas con la misma grafía ⇒ 1 miss + 1 hit."""
    parse_day_count.cache_clear()
    parse_day_count("ACT/365.25")
    parse_day_count("ACT/365.25")
    info = parse_day_count.cache_info()
    assert info.hits >= 1, f"el memo no pegó: {info}"
    assert info.misses == 1, f"se re-parseó la misma grafía: {info}"


def test_parse_day_count_memo_is_bounded():
    """Cap de tamaño: el dominio real son 4-5 grafías, pero la grafía viene de
    datos (ABM/Excel) → un memo sin techo sería un leak dirigible por input."""
    assert parse_day_count.cache_info().maxsize is not None


@pytest.mark.parametrize("raw", _ALIASES)
def test_parse_day_count_memo_no_mueve_resultados(raw):
    """Oráculo de pureza: memo LIMPIO vs memo POBLADO ⇒ igualdad EXACTA."""
    parse_day_count.cache_clear()
    cold = parse_day_count(raw)
    warm = parse_day_count(raw)
    assert cold is warm
    assert isinstance(cold, DayCount)


@pytest.mark.parametrize("raw", [["ACT/365"], {"a": 1}, {"30/360"}, bytearray(b"x")])
def test_parse_day_count_no_lanza_con_argumento_no_hasheable(raw):
    """Contrato del docstring: NUNCA lanza. `lru_cache` pelado tira
    `TypeError: unhashable type: 'list'` ⇒ hay que degradar al camino sin memo,
    devolviendo EXACTAMENTE lo que devolvía la implementación (str(raw) → parseo)."""
    from core.domain.daycount import _parse_day_count_impl
    assert parse_day_count(raw) is _parse_day_count_impl(raw)


def test_parse_day_count_catalogo_real_bit_identico():
    """Todas las grafías `day_count` del catálogo VIVO: memo limpio == memo poblado."""
    from core.infrastructure.db.catalog_repository import CatalogRepository

    insts = CatalogRepository().get_all_instruments()
    assert insts, "catálogo vacío"

    parse_day_count.cache_clear()
    cold = [parse_day_count(i.day_count) for i in insts]   # memo se va poblando
    parse_day_count.cache_clear()
    # segunda pasada, memo limpio de nuevo pero en ORDEN INVERSO (distinta secuencia
    # de misses/hits) → si el memo dependiera del orden, acá se ve.
    warm = list(reversed([parse_day_count(i.day_count) for i in reversed(insts)]))
    assert cold == warm
