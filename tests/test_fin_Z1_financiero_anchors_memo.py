"""Lote Z1 — `bond_detail._build_anchors` se memoiza por (mes, args).

HALLAZGO (quedó abierto del informe original). `cer_projection` arma las anclas
del 10º día hábil y después llama a `cer_return_scenarios`, que las vuelve a
armar con argumentos IDÉNTICOS: dos barridos completos del calendario BYMA
(`date_range_habil` sobre el rango hoy → vencimiento+40d, o sea años de días
hábiles) por cada request del popup de un bono CER.

El calendario es estático dentro del mes, así que el segundo barrido es puro
desperdicio de CPU en el thread pool. Memoizado por `((año, mes) de hoy, start,
end)`, con invalidación al cambiar de mes.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from apps.web import bond_detail
from core.domain.models import Cashflow, Instrument

_HOY = date(2026, 9, 3)
_VTO = date(2027, 6, 30)
_CER_BASE = 500.0


@pytest.fixture(autouse=True)
def _freeze_y_memo_limpio(monkeypatch):
    monkeypatch.setenv("MONITOR_AS_OF", _HOY.isoformat())
    bond_detail._ANCHORS_MEMO.clear()
    yield
    bond_detail._ANCHORS_MEMO.clear()


@pytest.fixture
def contador(monkeypatch):
    """Envuelve `date_range_habil` para contar los barridos del calendario."""
    real = bond_detail.date_range_habil
    llamadas: list = []

    def _spy(start, end):
        llamadas.append((start, end))
        return real(start, end)

    monkeypatch.setattr(bond_detail, "date_range_habil", _spy)
    return llamadas


class _Indices:
    def get_cer(self, d):
        return _CER_BASE * 1.02 ** ((d - date(2026, 1, 1)).days / 30.0)

    def get_tamar(self, d):
        return 30.0

    @property
    def _cache_tamar(self):
        return {date(2026, 9, 1): 30.0}


class _Repo:
    def __init__(self, inst):
        self._inst = inst

    def get_instrument_by_ticker(self, t):
        return self._inst if t == self._inst.ticker else None


def _lecer() -> Instrument:
    return Instrument(
        ticker="TZXJ7", short_name="LECER", instrument_type="LECER",
        emission_date=date(2026, 1, 15), maturity_date=_VTO,
        cer_base=_CER_BASE, cer_lag=10,
        cashflows=[Cashflow(date=_VTO, amortization=100.0, interest=0.0)],
    )


def _rango():
    """Los mismos args con los que llaman los dos call-sites del popup."""
    return (date(_HOY.year, _HOY.month, 1) - timedelta(days=5), _VTO + timedelta(days=40))


# ── El memo en sí ─────────────────────────────────────────────────────────────

def test_dos_llamadas_iguales_barren_el_calendario_una_sola_vez(contador):
    start, end = _rango()
    a1 = bond_detail._build_anchors(start, end)
    a2 = bond_detail._build_anchors(start, end)
    assert len(contador) == 1, f"barrió el calendario {len(contador)} veces"
    assert a2 is a1                      # mismo objeto: es el memo, no un recálculo
    assert a1, "el rango de prueba tiene que producir anclas"


def test_el_memo_no_cambia_el_resultado(contador):
    start, end = _rango()
    memoizado = bond_detail._build_anchors(start, end)
    bond_detail._ANCHORS_MEMO.clear()
    recalculado = bond_detail._build_anchors(start, end)
    assert recalculado == memoizado
    assert len(contador) == 2
    # y las anclas son de verdad el 10º día hábil del mes
    anchor_sep = memoizado[(2026, 9)]
    habiles = [d.date() if hasattr(d, "date") else d
               for d in bond_detail.date_range_habil(
                   date(2026, 9, 1).isoformat(), date(2026, 9, 30).isoformat())]
    assert anchor_sep == sorted(habiles)[9]


def test_argumentos_distintos_no_comparten_entrada(contador):
    start, end = _rango()
    bond_detail._build_anchors(start, end)
    bond_detail._build_anchors(start, end + timedelta(days=400))
    assert len(contador) == 2, "un rango distinto tiene que recalcular"


def test_el_memo_caduca_al_cambiar_de_mes(contador, monkeypatch):
    start, end = _rango()
    bond_detail._build_anchors(start, end)
    assert len(contador) == 1
    monkeypatch.setenv("MONITOR_AS_OF", "2026-10-01")
    bond_detail._build_anchors(start, end)
    assert len(contador) == 2, "el memo no se invalidó al cambiar el mes"
    # y las entradas del mes viejo se tiran (el memo no crece sin techo)
    assert all(k[0] == (2026, 10) for k in bond_detail._ANCHORS_MEMO)


# ── El caso real: un request del popup ────────────────────────────────────────

def test_cer_projection_arma_las_anclas_una_sola_vez_por_request(contador):
    """`cer_projection` + su llamada interna a `cer_return_scenarios` compartían
    argumentos y barrían dos veces. Ahora es un solo barrido por request."""
    inst = _lecer()
    out = bond_detail.cer_projection(
        "TZXJ7", _Repo(inst), None, _Indices(), None,
        price_dirty=95.0, settlement_lag=1, custom_infl_monthly=0.02,
    )
    assert out["is_cer"] is True
    assert out["months"], "el escenario tiene que producir meses proyectados"
    assert len(contador) == 1, (
        f"el popup barrió el calendario {len(contador)} veces: {contador}")
