"""Golden EXTERNO e independiente del motor para un BONCER: TX28 (BONCER 2,25 % 2028).

Primer golden de la familia CER (el inventario de `docs/convenciones-financieras.md`
tenía 0). Verifica que la TIR real, el V.Téc, la paridad y la MD del motor coinciden con
lo que publica un tercero que NO usa este código.

PROCEDENCIA (agents.md §0.1.14) — fuente primaria:
  Banco Hipotecario, «Informe Diario» (PDF público,
  https://www.hipotecario.com.ar/media/InformeDiario.pdf), tabla CER, columnas
  `Ticker Emisor Vencimiento Precio TIR Modified Duration (*) Paridad Días`:
  **TX28 · Precio $ 1,719.00 · TIR 8.82% · MD 1.08 · Paridad 93.14% · Días 797**.
  Fecha del dato: **2026-09-03** (el PDF dice 03/09/2026 en el encabezado y «Datos al
  04/09/2026» al pie; 1719 es EXACTAMENTE el cierre 24hs de BYMA del 03/09/2026 según la
  serie oficial `TX28 24HS` —03/09 c=1719, 04/09 c=1728.5—, así que el precio manda:
  rueda 2026-09-03, liquidación 24hs = T+1 = 2026-09-04). Precio en ARS por 100 VN,
  dirty. La fuente NO declara base de la TIR, plazo de liquidación, day-count ni fecha
  del CER; el V.Téc no se publica y se deriva de paridad = precio / V.Téc → 1845.61.
Puntos secundarios (intradía 2026-09-07, mercado abierto): Bonistas.com @1738 (24hs) →
  TIR 8.11 % / paridad 93.89 % / V.Téc 1851.03 / MD 1.08; Docta @1737.50 → TIR 8,13 % /
  paridad 93,86 % / V.Téc 1851.15. Dos calculadoras distintas difieren 0,12 ARS de V.Téc
  (0,006 %) y 2 bp de TIR (la mitad explicada por los 0,50 de precio): eso acota lo que
  es razonable exigirle al motor.
Insumos congelados en `tests/fixtures/tx28_2026-09-03.json`: la fila y los 17 flujos de
  TX28 volcados de la `catalog.db` local en modo SOLO LECTURA (el test no toca la DB
  viva), y la serie CER de BCRA (API v4.0, variable 30, captura 2026-09-07) para el rango
  que el motor necesita más el CER del 2020-08-21 que fija el `cer_base`.

QUÉ CONVENCIÓN CUADRA (probado antes de fijar tolerancias, ver la sonda en el commit):
  el motor reproduce la fuente con la cadena `settle T+1 (2026-09-04) → CER de 10 hábiles
  antes (2026-08-21 = 826.21153858876) → deflactar el precio → XIRR 30/360 sobre los flujos
  base`. Motor: TIR 8,822 % · paridad 93,140 % · MD 1,081 · V.Téc 1845,62 (a 0,2 bp,
  0,000 pp, 0,001 y 0,01 ARS de la fuente). La TIR del motor es REAL (sobre CER) y
  efectiva anual: coincide con el 8,82 % publicado, o sea la fuente también publica TEA
  real. Las alternativas quedan AFUERA de la tolerancia y el test lo fija
  (`test_el_corte_discrimina_las_convenciones`): T+0 → 8,73 %; `cer_lag=0` → 9,73 %;
  CER +1 % → 9,79 %; ACT/365.25 → paridad 93,00 % (el accrued 30/360 = 0,359375 vs 0,4326
  de ACT: la paridad discrimina el day-count aunque la TIR casi no —8,83 %—).

TOLERANCIAS (la fuente redondea a 2 decimales):
  TIR ±1,5 bp (canónica de golden; el redondeo a 0,01 % vale ±0,5 bp, el motor está a
  0,2-0,5 bp) · paridad ±1e-4 (0,01 pp: ±0,5e-4 de redondeo + 0,5e-4 del día de accrued
  entre el camino popup —accrued al settle— y el del panel —accrued a la rueda—) · V.Téc
  derivado ±0,2 ARS (la paridad publicada a 2 decimales deja ±0,1 de incertidumbre en
  1719/0.9314) · V.Téc publicado por Bonistas ±0,01 (a 2 decimales) · MD ±0,01 (canónica).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from core.domain.conventions import cer_reference_date, settlement_byma_date
from core.domain.models import Cashflow, Instrument, MarketSnapshot
from core.domain.pricing import metrics
from core.domain.services import FinancialEngine

_FIXTURE = Path(__file__).parent / "fixtures" / "tx28_2026-09-03.json"
_FX = json.loads(_FIXTURE.read_text(encoding="utf-8"))
_CER = {date.fromisoformat(k): v for k, v in _FX["cer_bcra"]["serie"].items()}
_BH = _FX["cortes_externos"]["banco_hipotecario_2026-09-03"]

_RUEDA = date(2026, 9, 3)          # jueves hábil: fecha del precio 1719 (cierre 24hs)
_SETTLE = date(2026, 9, 4)         # T+1
_REF_CER = date(2026, 8, 21)       # settle − 10 hábiles
_PRECIO = _BH["precio_dirty_ars_por_100vn"]

_TOL_TIR = 1.5e-4
_TOL_PARIDAD = 1e-4
_TOL_MD = 1e-2


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setenv("MONITOR_AS_OF", _RUEDA.isoformat())


class _CerCongelado:
    """IndicesProvider con la serie BCRA de la fixture. Registra qué fechas le pide el
    motor y EXPLOTA ante una fecha fuera de la fixture: si un cambio de lag/liquidación
    lo manda a otra fecha, tiene que verse como fecha equivocada, no degradar en
    silencio a TIR nominal (que es lo que hace `CerStrategy` cuando `get_cer` da None)."""

    def __init__(self, scale: float = 1.0):
        self.scale = scale
        self.asked: list[date] = []

    def get_cer(self, target: date):
        self.asked.append(target)
        if target not in _CER:
            raise KeyError(f"CER {target} fuera de la fixture congelada")
        return _CER[target] * self.scale

    def get_tamar(self, target=None):
        return None


def _instrument(**overrides) -> Instrument:
    """Mismo mapeo que `catalog_repository._orm_to_domain`: fila + flujos (sin anclas)."""
    meta = _FX["instrument"]
    fields = dict(
        ticker=meta["ticker"], short_name=meta["short_name"],
        instrument_type=meta["instrument_type"], isin=meta["isin"],
        maturity_date=date.fromisoformat(meta["maturity_date"]),
        emission_date=date.fromisoformat(meta["emission_date"]),
        cer_base=meta["cer_base"], cer_lag=meta["cer_lag"], category=meta["category"],
        payment_frequency=meta["payment_frequency"], day_count=meta["day_count"],
        cashflows=tuple(
            Cashflow(date=date.fromisoformat(cf["fecha_pago"]),
                     amortization=cf["amortizacion"], interest=cf["cupon_interes"])
            for cf in _FX["cashflows"] if not cf["es_ancla"]
        ),
    )
    fields.update(overrides)
    return Instrument(**fields)


def _motor(rueda: date, price: float, *, settle_lag: int = 1, idx=None, **overrides) -> dict:
    """TIR / MD / V.Téc / paridad como los publica la app.

    `vt_popup`: camino de `bond_detail._live_metrics` (ref = settle, `settle_lag=0`:
    accrued y CER al mismo settle). `vt_panel`: camino de `generate_report` (ref = la
    rueda, `settle_lag=1`: accrued a la rueda, CER a la liquidación de la rueda − lag)."""
    idx = idx if idx is not None else _CerCongelado()
    inst = _instrument(**overrides)
    settle = settlement_byma_date(rueda, lag=settle_lag)
    snap = MarketSnapshot(instrument=inst, price=price)
    tir = FinancialEngine.calculate_tir(snap, idx, None, settle_date=settle)
    md = FinancialEngine.calculate_duration(snap, tir, settle_date=settle)
    vt_popup = FinancialEngine.calculate_technical_value(
        snap, idx, None, ref_date=settle, settle_lag=0)
    vt_panel = FinancialEngine.calculate_technical_value(
        snap, idx, None, ref_date=rueda, settle_lag=settle_lag)
    return dict(inst=inst, snap=snap, idx=idx, settle=settle, tir=tir, md=md,
                vt_popup=vt_popup, vt_panel=vt_panel,
                paridad_popup=price / vt_popup, paridad_panel=price / vt_panel)


# ── La fixture es coherente con BCRA y con la NT N°8/2024 ─────────────────────

def test_cer_base_es_el_cer_bcra_de_10_habiles_antes_de_la_emision():
    """`cer_base` 22.5439510896 (DB) = CER BCRA del 2020-08-21 (22.54395108959) y esa
    fecha es exactamente emisión (2020-09-04) − 10 hábiles con el calendario del repo."""
    inst = _instrument()
    assert cer_reference_date(inst.emission_date, inst.cer_lag) == date(2020, 8, 21)
    assert inst.cer_base == pytest.approx(_CER[date(2020, 8, 21)], abs=1e-9)


def test_la_fila_de_la_db_pasa_por_el_parser_compartido_sin_cambiar():
    """`raw_fields` (lo que guarda el ABM) → `build_instrument` (el ÚNICO parser, Excel y
    ABM) da el mismo instrumento que la fila materializada: tipo, cer_base, lag,
    day-count, frecuencia, fechas. Si esto se rompe, la fixture ya no es lo que la app
    construye y el golden probaría otro bono."""
    from core.infrastructure.repositories import build_instrument

    ref = _instrument()
    built = build_instrument(_FX["raw_fields"], _FX["instrument"]["sheet"], list(ref.cashflows))
    assert built is not None
    for campo in ("ticker", "instrument_type", "cer_base", "cer_lag", "day_count",
                  "payment_frequency", "maturity_date", "emission_date"):
        assert getattr(built, campo) == getattr(ref, campo), campo


def test_quedan_cinco_flujos_y_residual_50_al_settle():
    inst = _instrument()
    fut = inst.get_future_cashflows(_SETTLE)
    assert [cf.date for cf in fut] == [
        date(2026, 11, 9), date(2027, 5, 9), date(2027, 11, 9),
        date(2028, 5, 9), date(2028, 11, 9)]
    assert all(cf.amortization == pytest.approx(10.0) for cf in fut)
    assert metrics.residual_nominal(inst, _SETTLE) == pytest.approx(50.0)


def test_cadena_de_fechas_del_corte():
    """Rueda 2026-09-03 → T+1 = 2026-09-04 → CER de 10 hábiles antes = 2026-08-21."""
    assert settlement_byma_date(_RUEDA, lag=1) == _SETTLE
    assert cer_reference_date(_SETTLE, 10) == _REF_CER
    assert _CER[_REF_CER] == 826.21153858876


# ── Banco Hipotecario, 2026-09-03 @ 1719 ──────────────────────────────────────

def test_el_motor_lee_solo_el_cer_de_liquidacion_menos_10_habiles():
    m = _motor(_RUEDA, _PRECIO)
    assert set(m["idx"].asked) == {_REF_CER}, sorted(set(m["idx"].asked))


def test_tir_real_tea_vs_banco_hipotecario():
    m = _motor(_RUEDA, _PRECIO)
    assert m["tir"] == pytest.approx(_BH["tir"], abs=_TOL_TIR), f"TIR {m['tir']*100:.3f}%"


def test_vtec_y_paridad_vs_banco_hipotecario():
    """Paridad publicada 93.14 % ⇒ V.Téc implícito 1719/0.9314 = 1845.61. Lo dan los dos
    caminos de la app (popup: accrued al settle; panel: accrued a la rueda)."""
    m = _motor(_RUEDA, _PRECIO)
    vt_implicito = _PRECIO / _BH["paridad"]
    assert m["vt_popup"] == pytest.approx(vt_implicito, abs=0.2), m["vt_popup"]
    assert m["paridad_popup"] == pytest.approx(_BH["paridad"], abs=_TOL_PARIDAD)
    assert m["paridad_panel"] == pytest.approx(_BH["paridad"], abs=_TOL_PARIDAD)
    # Descomposición: base 30/360 = residual 50 + accrued 0.5625 × 115/180, × CER_ref/base.
    inst = m["inst"]
    assert metrics.accrued_interest(inst, _SETTLE) == pytest.approx(0.359375, abs=1e-12)
    ratio = _CER[_REF_CER] / inst.cer_base
    assert m["vt_popup"] == pytest.approx(50.359375 * ratio, rel=1e-12)


def test_md_vs_banco_hipotecario():
    m = _motor(_RUEDA, _PRECIO)
    assert m["md"] == pytest.approx(_BH["md"], abs=_TOL_MD), m["md"]


def test_round_trip_tir_precio():
    m = _motor(_RUEDA, _PRECIO)
    back = FinancialEngine.price_from_tir(m["snap"], m["tir"], m["idx"], None,
                                          settle_date=m["settle"])
    assert back == pytest.approx(_PRECIO, rel=1e-9)


# ── Bonistas / Docta, 2026-09-07 (intradía, CER del 2026-08-25) ───────────────

_SECUNDARIOS = [
    ("bonistas_2026-09-07", 0.01),   # V.Téc publicado a 2 decimales
    ("docta_2026-09-07", 0.15),      # difiere 0.12 de Bonistas (0.006 %): dos calculadoras
]


@pytest.mark.parametrize("clave,tol_vt", _SECUNDARIOS, ids=[s[0] for s in _SECUNDARIOS])
def test_puntos_secundarios_2026_09_07(clave, tol_vt):
    corte = _FX["cortes_externos"][clave]
    rueda = date(2026, 9, 7)
    assert settlement_byma_date(rueda, lag=1) == date(2026, 9, 8)
    assert cer_reference_date(date(2026, 9, 8), 10) == date(2026, 8, 25)
    m = _motor(rueda, corte["precio_dirty_ars_por_100vn"])
    assert set(m["idx"].asked) == {date(2026, 8, 25)}
    assert m["tir"] == pytest.approx(corte["tir"], abs=_TOL_TIR), f"TIR {m['tir']*100:.3f}%"
    assert m["vt_popup"] == pytest.approx(corte["vt"], abs=tol_vt), m["vt_popup"]
    assert m["paridad_popup"] == pytest.approx(corte["paridad"], abs=_TOL_PARIDAD)
    if "md" in corte:
        assert m["md"] == pytest.approx(corte["md"], abs=_TOL_MD), m["md"]


# ── El corte discrimina las convenciones (mutación codificada) ────────────────

@pytest.mark.parametrize("etiqueta,kwargs,metrica,min_gap", [
    ("liquidación T+0 en vez de T+1", dict(settle_lag=0), "tir", 5e-4),
    ("cer_lag=0 (CER del settle, sin los 10 hábiles)", dict(cer_lag=0), "tir", 5e-3),
    ("CER +1 %", dict(idx=_CerCongelado(scale=1.01)), "tir", 5e-3),
    ("CER −1 %", dict(idx=_CerCongelado(scale=0.99)), "tir", 5e-3),
    ("day-count ACT/365.25 en vez de 30/360", dict(day_count="ACT/365.25"), "paridad_popup", 1e-3),
    ("day-count ACT/365 en vez de 30/360", dict(day_count="ACT/365"), "paridad_popup", 1e-3),
])
def test_el_corte_discrimina_las_convenciones(etiqueta, kwargs, metrica, min_gap):
    """Cada alternativa plausible cae AFUERA de la tolerancia del golden: si alguna
    entrara, el golden no distinguiría esa convención y sería decorativo."""
    ref = _BH["tir"] if metrica == "tir" else _BH["paridad"]
    tol = _TOL_TIR if metrica == "tir" else _TOL_PARIDAD
    valor = _motor(_RUEDA, _PRECIO, **kwargs)[metrica]
    assert abs(valor - ref) > min_gap, f"{etiqueta}: {valor} vs {ref}"
    assert valor != pytest.approx(ref, abs=tol), etiqueta
