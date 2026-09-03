"""Auditoría R1 — `Instrument.is_ley_argentina` normaliza las grafías reales.

`is_ley_argentina` decide con qué dólar se pasa a USD la pata PESOS de una ON
hard-dollar (`HardDollarStrategy._peso_fx_rate`): **MEP** si es ley local, **CCL**
si es extranjera o sin dato. Con brecha MEP/CCL eso mueve precio, TIR y paridad.

El chequeo era `"ARGENTIN" in ley.upper()`, o sea sólo matcheaba
"Argentina"/"ARGENTINA". Los escritores del campo no son uno solo:

  · el form ABM y `on_catalog` escriben "Argentina" / "Extranjera";
  · `byma/universe.py` mapea "Ley Local" → "Argentina";
  · pero hay scripts que escribieron abreviaturas — en la catalog.db VIVA hay
    hoy 1 fila con `'ARG'` (y 18 con `'EXT'`), y `scratch/gen_prov_cba_caba.py`
    también emite `'ARG'`.

`'ARG'` NO matcheaba → una ON de ley argentina se valuaba al CCL en silencio.
"""
from __future__ import annotations

from datetime import date

import pytest

from core.domain.models import Instrument


def _inst(ley) -> Instrument:
    return Instrument(ticker="XXO", short_name="ON", instrument_type="HARD DOLLAR",
                      maturity_date=date(2030, 1, 1), ley_aplicable=ley, cashflows=[])


_LOCAL = ["Argentina", "ARGENTINA", "argentina", " Argentina ", "AR", "ARG",
          "Arg.", "Local", "LOCAL", "Ley Local", "Ley Argentina", "LEY AR"]
_EXTRANJERA = ["Extranjera", "EXTRANJERA", "EXT", "NY", "New York", "Nueva York",
               "Ley de Nueva York", "Inglaterra", None, "", "   "]


@pytest.mark.parametrize("ley", _LOCAL)
def test_grafias_de_ley_local(ley):
    assert _inst(ley).is_ley_argentina is True, ley


@pytest.mark.parametrize("ley", _EXTRANJERA)
def test_grafias_de_ley_extranjera_o_sin_dato(ley):
    assert _inst(ley).is_ley_argentina is False, ley


def test_arg_es_el_caso_que_existe_hoy_en_la_db_viva():
    """Guarda puntual del valor medido en la catalog.db del usuario."""
    assert _inst("ARG").is_ley_argentina is True


def test_la_ley_elige_mep_vs_ccl_en_el_pricing_de_la_pata_pesos():
    """El efecto: con brecha, la pata pesos de una ON 'ARG' se dolariza al MEP."""
    from core.domain.pricing.context import PricingContext
    from core.domain.pricing.strategies import HardDollarStrategy

    class _Fx:
        def get_mep_venta(self):
            return 1400.0

        def get_ccl_venta(self):
            return 1500.0

    ctx = PricingContext(settle=date(2026, 9, 4), indices=None, fx=_Fx())
    st = HardDollarStrategy()
    assert st._peso_fx_rate(_inst("ARG"), ctx) == 1400.0     # ley local → MEP
    assert st._peso_fx_rate(_inst("EXT"), ctx) == 1500.0     # extranjera → CCL
    assert st._peso_fx_rate(_inst(None), ctx) == 1500.0      # sin dato → CCL
