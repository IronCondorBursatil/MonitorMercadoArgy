"""Auditoría F_ops — el `ley_aplicable` de los scripts de alta usa el vocabulario canónico.

`Instrument.is_ley_argentina` (core/domain/models.py) es `"ARGENTIN" in ley.upper()`, y
con eso el MOTOR elige el FX de la pata pesos: MEP (ley argentina) vs CCL (extranjera)
— `HardDollarStrategy._peso_fx_rate` / `fx_legs.peso_leg_to_usd`. O sea que un literal
fuera del vocabulario ('AR', 'ARG', 'Local') no es cosmético: cae como ley EXTRANJERA y
la TIR/paridad/Current Yield de la pata ARS salen con el tipo de cambio equivocado.

`scripts/ingest_on_lms8o.py` declaraba `"ley_aplicable": "AR"` — Aluar Serie 8 es ley
ARGENTINA, y es el único de los 10 scripts de alta puntual que no usaba el literal
canónico (el resto ya escribe 'Argentina'; `ingest_on_iamc_2026_08` incluso traduce
`row["ley"] == "AR"` → "Argentina" antes de guardar). Es una regresión LATENTE: la fila
viva hoy dice 'Argentina' porque la escribió otro ingest, y se dispara al re-correr el
script (que es explícitamente re-ejecutable).
"""

from __future__ import annotations

import importlib

import pytest

# script → (¿la pata pesos va contra MEP?, motivo)
ALTAS = {
    "ingest_on_lms8o": (True, "Aluar Serie 8, ley argentina"),
    "ingest_on_mcc1o": (True, "ley argentina"),
    "ingest_on_pecno": (True, "ley argentina"),
    "ingest_on_vscpo": (True, "ley argentina"),
    "ingest_on_xmc1o": (True, "ley argentina"),
    "ingest_on_yfcio": (True, "ley argentina"),
    "ingest_on_ypc4o": (False, "YPF 2028, ley Nueva York → CCL"),
}
CANONICOS = {"Argentina", "Extranjera"}


def _ley(modname: str) -> str:
    mod = importlib.import_module(f"scripts.{modname}")
    return mod.FIELDS["ley_aplicable"]


@pytest.mark.parametrize("modname", sorted(ALTAS))
def test_usa_el_vocabulario_canonico(modname):
    """'Argentina' / 'Extranjera' — el mismo que ofrece el select del ABM."""
    assert _ley(modname) in CANONICOS


@pytest.mark.parametrize("modname", sorted(ALTAS))
def test_la_ley_declarada_elige_el_fx_correcto(modname):
    """Lo que realmente importa: el predicado que usa el motor."""
    from core.domain.models import Instrument

    espera_mep, motivo = ALTAS[modname]
    inst = Instrument(ticker="X", short_name="x", instrument_type="HARD DOLLAR",
                      ley_aplicable=_ley(modname))
    assert inst.is_ley_argentina is espera_mep, motivo
