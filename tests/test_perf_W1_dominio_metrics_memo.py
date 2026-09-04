"""W1 (dominio) — Fase 3, item 2: memo por instrumento de las funciones puras de
`pricing/metrics.py` (`discount_year_fractions`, `accrued_interest`, `period_bounds`).

Motivo: `discount_year_fractions` se computa 2x por instrumento y por ciclo (una vez
para la TIR y otra para la MD) recorriendo TODOS los cashflows con `year_fraction_to`.

El memo vive en un `PrivateAttr` de `Instrument` (mutable aunque el modelo sea
`frozen=True` — verificado empíricamente, ver `test_privateattr_es_mutable_en_frozen`).

TRAMPA CENTRAL que estos tests blindan: `model_copy(update={...})` COMPARTE los
valores de `__pydantic_private__` con el original (verificado). `recompute_as_tamar_puro`
clona con `instrument_type="PURO"`; si el memo se compartiera, un clon envenenaria al
original (BOPREAL descuenta 30/360, el clon PURO no).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.domain.models import Instrument
from core.domain.pricing import metrics

REF = date(2026, 6, 10)


# --------------------------------------------------------------------------- #
# Supuestos pydantic sobre los que se construye el memo (verificados, no asumidos)
# --------------------------------------------------------------------------- #

def test_privateattr_es_mutable_en_frozen(catalog_instruments):
    """El modelo es frozen pero su `__pydantic_private__` se puede mutar."""
    inst = catalog_instruments[0]
    priv = inst.__pydantic_private__
    assert priv is not None, "Instrument no declara PrivateAttr: el memo no tiene donde vivir"
    assert metrics._MEMO_SLOT in priv
    priv[metrics._MEMO_SLOT]["_probe"] = 1
    assert inst.__pydantic_private__[metrics._MEMO_SLOT]["_probe"] == 1
    del priv[metrics._MEMO_SLOT]["_probe"]


def test_model_copy_no_comparte_el_memo(catalog_instruments):
    """`Instrument.model_copy` debe darle al clon un memo PROPIO y vacio."""
    inst = catalog_instruments[0].model_copy()
    metrics.discount_year_fractions(inst, REF)
    assert inst.__pydantic_private__[metrics._MEMO_SLOT], "el memo del original quedo vacio"
    clone = inst.model_copy(update={"instrument_type": "PURO"})
    assert clone.__pydantic_private__[metrics._MEMO_SLOT] == {}, (
        "el clon HEREDO el memo del original: envenenamiento cruzado"
    )
    metrics.discount_year_fractions(clone, REF)
    assert (inst.__pydantic_private__[metrics._MEMO_SLOT]
            is not clone.__pydantic_private__[metrics._MEMO_SLOT])


# --------------------------------------------------------------------------- #
# El memo EXISTE y PEGA
# --------------------------------------------------------------------------- #

def test_discount_year_fractions_no_recomputa(monkeypatch, bono_con_cupones):
    """2 llamadas => las year-fractions se calculan UNA sola vez."""
    inst = bono_con_cupones
    calls = []
    orig = Instrument.year_fraction_to
    monkeypatch.setattr(Instrument, "year_fraction_to",
                        lambda self, t, r: (calls.append(1), orig(self, t, r))[1])
    metrics.discount_year_fractions(inst, REF)
    n1 = len(calls)
    assert n1 > 0
    metrics.discount_year_fractions(inst, REF)
    assert len(calls) == n1, (
        "se recomputaron las year-fractions: %d llamadas extra" % (len(calls) - n1))


def test_period_bounds_devuelve_el_mismo_objeto(bono_con_cupones):
    """El resultado es inmutable (date, Cashflow) => el memo lo comparte tal cual."""
    a = metrics.period_bounds(bono_con_cupones, REF)
    b = metrics.period_bounds(bono_con_cupones, REF)
    assert a is not None
    assert a is b, "period_bounds recomputo (tupla nueva): sin memo"


def test_accrued_interest_no_recomputa(monkeypatch, bono_con_cupones):
    """2 llamadas => `period_bounds` (su dependencia) se invoca UNA sola vez."""
    calls = []
    orig = metrics.period_bounds
    monkeypatch.setattr(metrics, "period_bounds",
                        lambda i, r: (calls.append(1), orig(i, r))[1])
    metrics.accrued_interest(bono_con_cupones, REF)
    n1 = len(calls)
    assert n1 == 1
    metrics.accrued_interest(bono_con_cupones, REF)
    assert len(calls) == n1, "accrued_interest recomputo: sin memo"


# --------------------------------------------------------------------------- #
# El memo NO puede corromper resultados
# --------------------------------------------------------------------------- #

def test_discount_year_fractions_devuelve_copias(bono_con_cupones):
    """Los call-sites hacen `[0.0] + yfs` (necesita LISTA) y podrian mutar el
    resultado: el memo NO puede devolver sus estructuras internas."""
    cfs1, yfs1 = metrics.discount_year_fractions(bono_con_cupones, REF)
    assert isinstance(yfs1, list) and isinstance(cfs1, list)
    assert [0.0] + yfs1                       # el patron de pricing/base.py:100
    yfs1.append(999.0)
    yfs1[0] = -1.0
    cfs1.clear()
    cfs2, yfs2 = metrics.discount_year_fractions(bono_con_cupones, REF)
    assert cfs2, "la mutacion externa vacio la lista memoizada"
    assert 999.0 not in yfs2 and yfs2[0] != -1.0, "el memo devolvio su lista interna"


def test_memo_invalida_por_ref_date(bono_con_cupones):
    inst = bono_con_cupones
    _, y0 = metrics.discount_year_fractions(inst, REF)
    _, y1 = metrics.discount_year_fractions(inst, REF + timedelta(days=30))
    assert y0 != y1, "cambiar ref_date no invalido el memo"


def test_clon_por_instrument_type_no_envenena_al_original(bopreal):
    """BOPREAL descuenta 30/360; su clon "PURO" cae a la convencion declarada.
    Sin el `instrument_type` en la clave (o con el memo compartido), el clon le
    escribiria al original las year-fractions equivocadas."""
    inst = bopreal
    esperado_orig = metrics._discount_year_fractions_uncached(inst, REF)[1]
    clone = inst.model_copy(update={"instrument_type": "PURO", "floor_rate_monthly": None})
    esperado_clon = metrics._discount_year_fractions_uncached(clone, REF)[1]
    assert esperado_orig != esperado_clon, "fixture inutil: las convenciones coinciden"

    assert metrics.discount_year_fractions(inst, REF)[1] == esperado_orig
    assert metrics.discount_year_fractions(clone, REF)[1] == esperado_clon
    assert metrics.discount_year_fractions(inst, REF)[1] == esperado_orig

    inst2 = inst.model_copy()          # memo virgen
    clone2 = inst2.model_copy(update={"instrument_type": "PURO", "floor_rate_monthly": None})
    assert metrics.discount_year_fractions(clone2, REF)[1] == esperado_clon
    assert metrics.discount_year_fractions(inst2, REF)[1] == esperado_orig


def test_clon_por_ticker_no_envenena(bono_con_cupones):
    """`expand_currency_legs` clona con `ticker` distinto: ninguna metrica depende
    del ticker, pero el memo tampoco puede filtrar entre patas."""
    inst = bono_con_cupones
    esperado = metrics._discount_year_fractions_uncached(inst, REF)[1]
    leg = inst.model_copy(update={"ticker": inst.ticker + "D"})
    assert metrics.discount_year_fractions(leg, REF)[1] == esperado
    assert metrics.discount_year_fractions(inst, REF)[1] == esperado


def test_memo_acotado(bono_con_cupones):
    """Cap de tamano: por dia se usan <=3 refs (hoy, T+1, CI). Un memo sin techo
    creceria con cada fecha que le pase la calculadora del popup."""
    inst = bono_con_cupones.model_copy()
    memo = inst.__pydantic_private__[metrics._MEMO_SLOT]
    for i in range(400):
        metrics.discount_year_fractions(inst, REF + timedelta(days=i))
        metrics.accrued_interest(inst, REF + timedelta(days=i))
    assert len(memo) <= metrics._MEMO_CAP, "memo sin techo: %d entradas" % len(memo)


# --------------------------------------------------------------------------- #
# ORACULO DE PUREZA — catalogo real, igualdad EXACTA (==, no approx)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("delta", [0, 1, 5])
def test_oraculo_pureza_catalogo_real(catalog_instruments, delta):
    """Para CADA instrumento: memo limpio vs memo poblado => igualdad EXACTA."""
    ref = REF + timedelta(days=delta)
    diffs = []
    for inst in catalog_instruments:
        virgen = inst.model_copy()                     # memo propio y vacio
        esperado = (
            metrics._period_bounds_uncached(virgen, ref),
            metrics._discount_year_fractions_uncached(virgen, ref),
            metrics._accrued_interest_uncached(virgen, ref),
        )
        probe = inst.model_copy()
        cold = (metrics.period_bounds(probe, ref),
                metrics.discount_year_fractions(probe, ref),
                metrics.accrued_interest(probe, ref))
        warm = cold
        for _ in range(2):
            warm = (metrics.period_bounds(probe, ref),
                    metrics.discount_year_fractions(probe, ref),
                    metrics.accrued_interest(probe, ref))
        if not (esperado == cold == warm):
            diffs.append(inst.ticker)
    assert not diffs, "el memo movio resultados en: %s" % diffs[:20]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def catalog_instruments():
    from core.infrastructure.db.catalog_repository import CatalogRepository
    insts = CatalogRepository().get_all_instruments()
    assert insts, "catalogo vacio"
    return insts


@pytest.fixture
def bono_con_cupones(catalog_instruments):
    """Bono ACT/* con cupones futuros y al menos un cupon pagado (ejercita el
    camino completo de accrued/period_bounds)."""
    for i in catalog_instruments:
        if (not i.is_30_360 and len(i.cashflows) >= 4
                and any(c.date > REF and c.interest > 0 for c in i.cashflows)
                and any(c.date <= REF for c in i.cashflows)):
            return i.model_copy()
    pytest.skip("sin bono ACT con cupones en el catalogo")


@pytest.fixture
def bopreal(catalog_instruments):
    """BOPREAL con `day_count` declarado ACT/365: `is_bopreal` FUERZA 30/360, asi
    que el clon "PURO" (mismo day_count declarado) descuenta ACT/365. Es el unico
    caso del motor donde `instrument_type` mueve la convencion de descuento, o sea
    exactamente el envenenamiento que la clave del memo tiene que evitar."""
    for i in catalog_instruments:
        if i.is_bopreal and len(i.cashflows) >= 3:
            base = i.model_copy(update={"day_count": "ACT/365"})
            assert base.is_30_360 and base.day_count_enum.value == "30/360"
            return base
    pytest.skip("sin BOPREAL en el catalogo")
