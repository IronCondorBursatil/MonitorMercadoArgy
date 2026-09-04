"""Fase 9 (lote W1) — cashflows deterministas en la DB: fila ANCLA + write-path sin reloj.

Dos agujeros de correctitud (NO de velocidad; el pricing es 0,07-0,14 s de un ciclo
de 3-14 s):

1. Los 14 TAMAR/DUAL/DUAL_CER_TAMAR no tienen NINGUNA fila en `cashflows` — su payoff
   es fórmula cerrada (`tamar_dual_payoff_at`), así que persistir un schedule nominal
   sería *incorrecto*. Pero sin filas quedan invisibles en `/cashflows` y no hay forma
   de distinguir "sin vencimiento cargado" de "payoff analítico". La fila **ancla**
   (`es_ancla=1`, monto 0 al vto) los hace visibles EN LA DB, y el filtro de
   `_orm_to_domain` la deja fuera del dominio → el pricing queda **bit-idéntico por
   construcción** (cashflows=() como hoy).

2. El write-path del ABM sintetizaba al guardar y `cashflow_synth` lee el RELOJ para
   resolver el step-up del cupón → **el schedule persistido dependía del día del alta**.
"""

from __future__ import annotations

from datetime import date

import pytest

from config.settings import settings
from core.infrastructure.db import engine as db_engine
from core.infrastructure.db.catalog_repository import CatalogRepository, init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import CashflowORM, InstrumentORM

_EMISION = date(2025, 6, 30)
_VTO = date(2027, 6, 30)


@pytest.fixture
def tmp_catalog(tmp_path):
    """Engine apuntado a una .db temporal VACÍA (no toca la catalog.db de la suite).

    ⚠ NO combinar con `TestClient(app)`: el boot de la app carga el singleton de
    `get_repo()` desde la DB que esté configurada y lo deja CACHEADO. Con el engine en
    una temporal vacía, ese cache sobrevive al teardown de la fixture y contamina a
    los tests siguientes (`test_web_app::test_health_boots_and_loads_catalog` pasó a
    ver 2 instrumentos en vez de ~91). Los tests de router de este archivo trabajan a
    propósito sobre el catálogo compartido y limpian lo que crean."""
    db_engine.configure(tmp_path / "w1_ancla.db")
    init_db()
    try:
        yield
    finally:
        db_engine.configure(settings.catalog_db)


def _add_bond(ticker: str, itype: str, cashflows, sheet: str = "TAMAR") -> None:
    """Alta cruda por ORM. `cashflows` = [(fecha, amort, cupon, es_ancla)]."""
    with SessionLocal.begin() as s:
        orm = InstrumentORM(ticker=ticker, short_name=ticker, instrument_type=itype,
                            maturity_date=_VTO, emission_date=_EMISION,
                            day_count="30/360", sheet=sheet)
        orm.cashflows = [
            CashflowORM(ticker=ticker, fecha_pago=d, amortizacion=a,
                        cupon_interes=i, es_ancla=anc)
            for d, a, i, anc in cashflows
        ]
        s.add(orm)


# --------------------------------------------------------------------------- #
# 1 + 2 · La fila ancla vive en el ORM y NO entra al dominio.
# --------------------------------------------------------------------------- #

def test_el_ancla_no_llega_al_dominio(tmp_catalog):
    """Un PURO con SOLO la fila ancla sigue llegando al dominio con cashflows=(),
    exactamente como los 14 TAMAR de hoy → el pricing no puede moverse."""
    _add_bond("TTM1", "PURO", [(_VTO, 0.0, 0.0, True)])
    inst = CatalogRepository(auto_seed=False).get_instrument_by_ticker("TTM1")
    assert inst is not None
    assert inst.cashflows == ()


def test_el_ancla_no_contamina_un_schedule_real(tmp_catalog):
    """Si un bono con flujos reales tuviera además un ancla, el dominio ve SOLO
    los flujos reales (el filtro es por fila, no por bono)."""
    _add_bond("TTM2", "BONAR", [(date(2026, 6, 30), 50.0, 1.5, False),
                                (_VTO, 50.0, 1.5, False),
                                (_VTO, 0.0, 0.0, True)], sheet="Soberanos")
    inst = CatalogRepository(auto_seed=False).get_instrument_by_ticker("TTM2")
    assert inst is not None
    assert [(cf.date, cf.amortization, cf.interest) for cf in inst.cashflows] == [
        (date(2026, 6, 30), 50.0, 1.5), (_VTO, 50.0, 1.5),
    ]


def test_alter_add_column_forward_only_sobre_una_db_vieja(tmp_path):
    """`es_ancla` entra por ALTER ADD COLUMN sobre una DB PREEXISTENTE (sin la
    columna) y las filas viejas quedan como flujo real (0/False). Forward-only:
    nada se dropea, nada se pierde."""
    import sqlalchemy as sa

    db = tmp_path / "vieja.db"
    eng = sa.create_engine(f"sqlite:///{db}")
    with eng.begin() as conn:   # schema PRE-ancla, a mano
        conn.exec_driver_sql(
            "CREATE TABLE cashflows (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ticker VARCHAR NOT NULL, fecha_pago DATE NOT NULL, "
            "amortizacion FLOAT NOT NULL, cupon_interes FLOAT NOT NULL)")
        conn.exec_driver_sql(
            "INSERT INTO cashflows (ticker, fecha_pago, amortizacion, cupon_interes) "
            "VALUES ('VIEJO', '2027-06-30', 100.0, 2.5)")
    eng.dispose()

    db_engine.configure(db)
    try:
        init_db()                                  # reconcilia con el ORM
        with SessionLocal() as s:
            rows = s.execute(sa.select(CashflowORM)).scalars().all()
            assert len(rows) == 1                  # la fila vieja sobrevive
            assert rows[0].amortizacion == 100.0
            assert not rows[0].es_ancla            # default: flujo real
    finally:
        db_engine.configure(settings.catalog_db)


# --------------------------------------------------------------------------- #
# 3 · ANALYTIC_PAYOFF_TYPES verificado CONTRA el registry (no contra una lista
#     escrita de memoria): son exactamente los tipos que el registry rutea a una
#     strategy de payoff cerrado, la que NO consume `inst.cashflows`.
# --------------------------------------------------------------------------- #

def test_analytic_payoff_types_coincide_con_el_registry():
    from core.domain.instrument_groups import (
        ANALYTIC_PAYOFF_TYPES, BOND_TYPES, has_closed_form_payoff,
    )
    from core.domain.models import Instrument
    from core.domain.pricing.registry import strategy_for
    from core.domain.pricing.strategies import DualCerTamarStrategy, TamarStrategy

    assert ANALYTIC_PAYOFF_TYPES == frozenset({"PURO", "DUAL", "DUAL_CER_TAMAR"})

    cerradas = (TamarStrategy, DualCerTamarStrategy)
    for t in BOND_TYPES:
        inst = Instrument(ticker="X", short_name="X", instrument_type=t)
        analitica = isinstance(strategy_for(inst), cerradas)
        assert analitica == (t in ANALYTIC_PAYOFF_TYPES), t
        assert analitica == has_closed_form_payoff(t), t


def test_has_closed_form_payoff_normaliza_y_tolera_basura():
    from core.domain.instrument_groups import has_closed_form_payoff
    assert has_closed_form_payoff(" dual_cer_tamar ")
    assert not has_closed_form_payoff(None)
    assert not has_closed_form_payoff("")
    assert not has_closed_form_payoff("BONAR")


# --------------------------------------------------------------------------- #
# ORÁCULO DE ACEPTACIÓN — el MISMO instrumento con y sin fila ancla tiene que dar
# period_bounds / residual_nominal / calculate_tir / technical_value IDÉNTICOS.
# Es la red que protege el invariante #1: si el ancla moviera un número, el diseño
# (marcarla en el ORM + filtrarla en _orm_to_domain) estaría mal.
# --------------------------------------------------------------------------- #

class _IndicesStub:
    """TAMAR 30% TNA constante + CER creciente 2%/mes desde 700 al 2025-06-30."""

    def get_tamar(self, d):
        return 30.0

    def get_cer(self, d):
        return 700.0 * (1.02 ** ((d - _EMISION).days / 30.0))

    @property
    def _cache_tamar(self):
        return {date(2026, 9, 1): 30.0}


def _metricas(inst, indices):
    from core.domain.models import MarketSnapshot
    from core.domain.services import FinancialEngine as FE

    ref = date(2026, 9, 4)
    snap = MarketSnapshot(instrument=inst, price=133.7)
    tir = FE.calculate_tir(snap, indices, None, settle_date=ref)
    return {
        "period_bounds": FE._period_bounds(inst, ref),
        "residual_nominal": FE.residual_nominal(inst, ref),
        "tir": tir,
        "technical_value": FE.calculate_technical_value(snap, indices, None, ref_date=ref),
        "duration": FE.calculate_duration(snap, tir, settle_date=ref),
        "accrued": FE.accrued_interest(inst, ref),
    }


@pytest.mark.parametrize("itype", ["PURO", "DUAL", "DUAL_CER_TAMAR"])
@pytest.mark.parametrize("con_indices", [True, False])
def test_oraculo_el_ancla_no_mueve_ninguna_metrica(tmp_catalog, itype, con_indices):
    """Con índices (camino de payoff cerrado) y SIN índices (fall-through vanilla,
    donde el motor SÍ mira los cashflows): las 6 métricas tienen que dar igual."""
    _add_bond("SINANC", itype, [])
    _add_bond("CONANC", itype, [(_VTO, 0.0, 0.0, True)])
    with SessionLocal.begin() as s:   # el DUAL_CER_TAMAR necesita su riel CER
        for o in s.query(InstrumentORM).all():
            o.cer_base, o.cer_spread, o.spread_rate = 700.0, 0.04, 0.05
            o.floor_rate_monthly = 0.022

    repo = CatalogRepository(auto_seed=False)
    indices = _IndicesStub() if con_indices else None
    i_sin, i_con = (repo.get_instrument_by_ticker("SINANC"),
                    repo.get_instrument_by_ticker("CONANC"))
    # El MECANISMO (no la coincidencia numérica): al motor le llega el mismo objeto.
    # Un ancla de monto 0 al vto resulta además INERTE para estas 6 métricas aunque
    # se colara —lo verifiqué revirtiendo el filtro—, así que sin esta línea el
    # oráculo pasaría igual y no probaría nada.
    assert i_con.cashflows == i_sin.cashflows == ()
    sin_ancla = _metricas(i_sin, indices)
    con_ancla = _metricas(i_con, indices)
    assert con_ancla == sin_ancla
    # y el oráculo no es trivial: con índices el motor devuelve números de verdad
    if con_indices:
        assert con_ancla["tir"] is not None and con_ancla["technical_value"] is not None


# --------------------------------------------------------------------------- #
# 4 · WRITE-PATH DETERMINISTA. El synth queda como PREVIEW; el POST manda el
#     schedule. Sin esto, `cashflow_synth` leía el reloj (step-up del cupón) y el
#     schedule que quedaba en la DB dependía del DÍA DEL ALTA.
# --------------------------------------------------------------------------- #

_TAMAR_FIELDS = {
    "ticker_ars": "TTX9", "short_name": "TEST TAMAR", "tipo": "PURO",
    "fecha_emision": "2025-06-30", "fecha_vencimiento": "2027-06-30",
    "base calculo": "30/360", "spread": "0.05",
}

# Step-up: el cupón vigente depende del "hoy" del dominio (0.63 hasta 2027, 1.18 después).
_STEPUP_FIELDS = {
    "ticker_ars": "TSU9", "short_name": "TEST STEP-UP", "tipo": "BONAR",
    "fecha_emision": "2025-06-30", "fecha_vencimiento": "2029-06-30",
    "cupon anual %": "2024-12-31:0.63;2027-12-31:1.18",
    "frecuencia pagos": "2", "base calculo": "ACT/365.25",
    "tipo amortizacion": "bullet",
}


def _rows(ticker: str):
    """Filas CRUDAS de la DB (incluida el ancla) del bono `ticker`."""
    with SessionLocal() as s:
        orm = s.get(InstrumentORM, ticker)
        if orm is None:
            return None
        return [(cf.fecha_pago, cf.amortizacion, cf.cupon_interes, bool(cf.es_ancla))
                for cf in orm.cashflows]


def test_alta_analitica_persiste_solo_la_fila_ancla(tmp_catalog):
    from apps.web import instruments_abm as abm

    abm.save_instrument("TAMAR", dict(_TAMAR_FIELDS))
    assert _rows("TTX9") == [(_VTO, 0.0, 0.0, True)]
    # y el dominio la sigue viendo SIN cashflows (invariante #1)
    assert CatalogRepository(auto_seed=False).get_instrument_by_ticker("TTX9").cashflows == ()


def test_alta_analitica_ignora_un_schedule_explicito(tmp_catalog):
    """Un TAMAR no puede tener schedule nominal: su payoff es fórmula cerrada. Si
    el form manda filas igual, se guarda el ancla y nada más — si se persistieran,
    llegarían al dominio (no son ancla) y CAMBIARÍAN el pricing de esos 14 bonos."""
    from apps.web import instruments_abm as abm

    abm.save_instrument("TAMAR", dict(_TAMAR_FIELDS),
                        [{"date": "2026-06-30", "amortization": "50", "interest": "3"},
                         {"date": "2027-06-30", "amortization": "50", "interest": "3"}])
    assert _rows("TTX9") == [(_VTO, 0.0, 0.0, True)]


def test_alta_analitica_exige_vencimiento(tmp_catalog):
    from apps.web import instruments_abm as abm

    fields = dict(_TAMAR_FIELDS)
    fields.pop("fecha_vencimiento")
    with pytest.raises(ValueError, match="necesita fecha de VENCIMIENTO"):
        abm.save_instrument("TAMAR", fields)
    assert _rows("TTX9") is None          # no quedó nada a medias


def test_alta_normal_sin_flujos_es_rechazada_con_mensaje_accionable(tmp_catalog):
    """Antes esto era un WARNING silencioso que dejaba un bono IMPRICEABLE en la DB."""
    from apps.web import instruments_abm as abm

    fields = dict(_STEPUP_FIELDS)
    fields.pop("cupon anual %")            # sin cupón el synth devolvía [] y guardaba igual
    with pytest.raises(ValueError) as ei:
        abm.save_instrument("Soberanos", fields)
    msg = str(ei.value)
    assert "flujo de fondos" in msg.lower()
    assert "previsualizar" in msg.lower()  # dice EXACTAMENTE qué hacer
    assert _rows("TSU9") is None


def test_write_path_no_lee_el_reloj(tmp_catalog, monkeypatch):
    """El MISMO alta guardada con dos relojes distintos tiene que dejar el MISMO
    schedule. Con el synth en el write-path, el step-up resolvía 0.63% con el reloj
    de 2026 y 1.18% con el de 2028 → dos schedules distintos para el mismo form."""
    from apps.web import instruments_abm as abm
    from core.domain import cashflow_synth

    def _guardar(reloj: date, ticker: str, cfs=None):
        """Alta LIMPIA (ticker propio: si se reusara el mismo, el 2º save heredaría
        los flujos del 1º y el reloj no jugaría) bajo el reloj `reloj`."""
        monkeypatch.setattr(cashflow_synth, "_domain_today", lambda: reloj)
        fields = dict(_STEPUP_FIELDS, ticker_ars=ticker)
        try:
            abm.save_instrument("Soberanos", fields, cfs)
            return ("ok", _rows(ticker))
        except ValueError as e:
            return ("rechazado", str(e).replace(ticker, "<TK>"))  # el ticker no es el punto

    assert _guardar(date(2026, 1, 1), "TSU9") == _guardar(date(2028, 1, 1), "TSV9")

    # …y con el schedule explícito (el que el preview mostró) tampoco: se persiste
    # EXACTAMENTE lo que mandó el form, con cualquier reloj.
    schedule = [{"date": "2027-06-30", "amortization": "0", "interest": "0.315"},
                {"date": "2029-06-30", "amortization": "100", "interest": "0.315"}]
    estado, filas = _guardar(date(2026, 1, 1), "TSW9", schedule)
    assert estado == "ok"
    assert filas == [(date(2027, 6, 30), 0.0, 0.315, False),
                     (date(2029, 6, 30), 100.0, 0.315, False)]
    assert _guardar(date(2028, 1, 1), "TSX9", schedule) == (estado, filas)


# --------------------------------------------------------------------------- #
# 4b · El FORM manda el schedule (consecuencia de sacar el reloj del write-path).
#      La tabla de flujos ahora vive DENTRO del <form> de /abm/save y se puebla con
#      POST /abm/preview_cashflows.
# --------------------------------------------------------------------------- #

_ON_FIELDS = {
    "sheet": "Obligaciones_Negociables",
    "ticker_ars": "W1F0O", "ticker_mep": "W1F0D", "ticker_ccl": "",
    "short_name": "W1 FORM TEST", "tipo": "HARD DOLLAR",
    "fecha_emision": "2025-07-22", "fecha_vencimiento": "2027-07-22",
    "cupon anual %": "7.5", "frecuencia pagos": "2",
    "base calculo": "ACT/365", "tipo amortizacion": "bullet",
}


def test_el_form_de_alta_trae_la_tabla_de_flujos_y_el_preview():
    """Antes la tabla sólo existía EDITANDO (`{% if ticker %}`) y en un <form> aparte:
    un alta no tenía cómo mandar el schedule."""
    from fastapi.testclient import TestClient

    from apps.web.app import app

    with TestClient(app) as c:
        html = c.get("/abm/form?sheet=Obligaciones_Negociables").text
    assert "/abm/preview_cashflows" in html          # el botón ⟳ que la puebla
    assert 'hx-post="/abm/save"' in html
    # la tabla está DENTRO del form del save: htmx incluye el form MÁS CERCANO en todo
    # POST, así que las filas viajan sin depender de un hx-include.
    cuerpo = html.split('hx-post="/abm/save"', 1)[1].split("</form>", 1)[0]
    assert 'id="abm-cf-body"' in cuerpo
    # sin <form> anidado (HTML inválido): la tabla dejó de tener el suyo.
    assert "<form" not in cuerpo
    # y el after-request del form filtra por `detail.elt`: htmx:afterRequest BURBUJEA
    # desde los botones internos, así que sin el filtro un preview cerraría el cajón.
    assert "event.detail.elt===this" in html


def test_el_form_de_edicion_tambien_manda_el_schedule():
    """En edición la tabla ya existía, pero en un <form> aparte: sus filas NO viajaban
    en el POST de /abm/save."""
    from fastapi.testclient import TestClient

    from apps.web.app import app

    with TestClient(app) as c:
        html = c.get("/abm/form?sheet=Soberanos&key=AL30").text
    cuerpo = html.split('hx-post="/abm/save"', 1)[1].split("</form>", 1)[0]
    assert cuerpo.count('name="cf_date"') > 0        # las filas del bono, en el form
    assert 'name="cf_ticker"' in cuerpo              # y el botón de "sólo flujos" sigue
    assert "/abm/cashflows" in cuerpo


def test_router_preview_cashflows_devuelve_filas_editables():
    from fastapi.testclient import TestClient

    from apps.web.app import app

    with TestClient(app) as c:
        r = c.post("/abm/preview_cashflows", data=_ON_FIELDS)
    assert r.status_code == 200
    assert r.text.count('name="cf_date"') == 4        # 2 años, semestral
    assert "2027-07-22" in r.text


def test_router_save_persiste_EXACTAMENTE_el_schedule_del_form():
    """El POST manda las filas del preview y en la DB queda eso — ni un synth nuevo
    (que dependería del reloj) ni un schedule distinto."""
    from fastapi.testclient import TestClient

    from apps.web.app import app
    from apps.web.deps import get_repo

    data = {**_ON_FIELDS,
            "cf_date": ["2026-07-22", "2027-07-22"],
            "cf_amort": ["0", "100"], "cf_interest": ["7.5", "7.5"]}
    with TestClient(app) as c:
        try:
            r = c.post("/abm/save", data=data)
            assert r.status_code == 200 and "No se guard" not in r.text
            inst = get_repo().get_instrument_by_ticker("W1F0D")
            assert inst is not None
            assert [(cf.date, cf.amortization, cf.interest) for cf in inst.cashflows] == [
                (date(2026, 7, 22), 0.0, 7.5), (date(2027, 7, 22), 100.0, 7.5),
            ]
        finally:
            c.delete("/abm/instrument/W1F0O")


def test_router_save_sin_schedule_no_deja_un_bono_impriceable():
    from fastapi.testclient import TestClient

    from apps.web.app import app
    from apps.web.deps import get_repo

    with TestClient(app) as c:
        try:
            r = c.post("/abm/save", data=dict(_ON_FIELDS))
            assert r.status_code == 200
            assert "No se guard" in r.text and "FLUJO DE FONDOS" in r.text
            assert get_repo().get_instrument_by_ticker("W1F0D") is None
        finally:
            c.delete("/abm/instrument/W1F0O")


def test_save_cashflows_no_puede_romper_un_tipo_analitico(tmp_catalog):
    """`save_cashflows` es la OTRA puerta de escritura de flujos. Sin guard, guardar
    filas a mano sobre un TAMAR las persiste SIN marca de ancla → llegan al dominio y
    cambian el pricing de esos 14 bonos (justo lo que el ancla evita)."""
    from apps.web import instruments_abm as abm

    abm.save_instrument("TAMAR", dict(_TAMAR_FIELDS))
    with pytest.raises(ValueError, match="fórmula cerrada"):
        abm.save_cashflows("TTX9", [{"date": "2026-06-30", "amortization": "50",
                                     "interest": "3"}])
    assert _rows("TTX9") == [(_VTO, 0.0, 0.0, True)]
    assert CatalogRepository(auto_seed=False).get_instrument_by_ticker("TTX9").cashflows == ()


# --------------------------------------------------------------------------- #
# 5b · LECTURA del form. El ancla es una fila del ORM, no un pago: no puede salir
#      por la tabla EDITABLE del cajón disfrazada de flujo. Y para un tipo analítico
#      la tabla tampoco puede caer al synth: mostraría un schedule que `save_instrument`
#      descarta a propósito — el form le mentiría al operador.
# --------------------------------------------------------------------------- #

def test_el_form_de_un_analitico_no_ofrece_el_ancla_como_flujo_editable(tmp_catalog):
    """Con el ancla ya en la DB, `get_instrument` la devolvía como una fila
    `vto / 0.000000 / 0.000000` editable, contra el texto del propio form
    ("esta tabla queda vacía a propósito")."""
    from apps.web import instruments_abm as abm

    abm.save_instrument("TAMAR", dict(_TAMAR_FIELDS))
    assert _rows("TTX9") == [(_VTO, 0.0, 0.0, True)]          # el ancla ESTÁ en la DB
    got = abm.get_instrument("TTX9")
    assert got["cashflows"] == []                             # …y NO sale por el form
    assert got["cashflows_source"] == "analitico"


def test_el_form_de_un_analitico_tampoco_sintetiza(tmp_catalog):
    """Un analítico SIN ancla (fila cruda, o una DB previa al backfill) tampoco puede
    caer al synth: `save_instrument` descarta ese schedule, así que ofrecerlo es
    ofrecer trabajo que se va a tirar."""
    from apps.web import instruments_abm as abm

    _add_bond("TTM9", "PURO", [])
    got = abm.get_instrument("TTM9")
    assert got["cashflows"] == []
    assert got["cashflows_source"] == "analitico"


def test_un_ancla_huerfana_no_se_muestra_como_flujo(tmp_catalog):
    """Defensa en profundidad: si un bono dejó de ser analítico y le quedó el ancla,
    esa fila sigue sin ser un pago — no puede aparecer en la tabla editable."""
    from apps.web import instruments_abm as abm

    _add_bond("TTM8", "BONAR", [(date(2026, 6, 30), 50.0, 1.5, False),
                                (_VTO, 50.0, 1.5, False),
                                (_VTO, 0.0, 0.0, True)], sheet="Soberanos")
    got = abm.get_instrument("TTM8")
    assert [c["date"] for c in got["cashflows"]] == ["2026-06-30", _VTO.isoformat()]
    assert got["cashflows_source"] == "sheet"


def test_el_form_de_un_bono_normal_no_cambia(tmp_catalog):
    """Contraprueba: el camino de siempre (flujos reales) sigue igual."""
    from apps.web import instruments_abm as abm

    _add_bond("TTM7", "BONAR", [(_VTO, 100.0, 2.5, False)], sheet="Soberanos")
    got = abm.get_instrument("TTM7")
    assert got["cashflows"] == [{"date": _VTO.isoformat(), "amortization": 100.0,
                                 "interest": 2.5}]
    assert got["cashflows_source"] == "sheet"


def test_router_el_cajon_de_un_analitico_abre_con_la_tabla_vacia():
    """End-to-end sobre un analítico que SÍ tiene ancla en la DB (si no, el test
    pasaría por vacío: el synth de un TAMAR sin cupón ya devuelve []). Contraprueba
    en el mismo test: un bono normal del catálogo SÍ trae sus filas."""
    from fastapi.testclient import TestClient

    from apps.web import instruments_abm as abm
    from apps.web.app import app

    abm.save_instrument("TAMAR", dict(_TAMAR_FIELDS))
    try:
        assert _rows("TTX9") == [(_VTO, 0.0, 0.0, True)]       # el ancla está
        with TestClient(app) as c:
            html = c.get("/abm/form?sheet=TAMAR&key=TTX9").text
            normal = c.get("/abm/form?sheet=Soberanos&key=AL30").text
    finally:
        abm.delete_instrument("TTX9")
    cuerpo = html.split('hx-post="/abm/save"', 1)[1].split("</form>", 1)[0]
    assert 'name="cf_date"' not in cuerpo                      # ni una fila editable
    assert "fórmula cerrada" in html or "analítico" in html or "ancla" in html
    # el test no pasa por vacío: el mismo endpoint SÍ trae filas para un bono normal
    assert normal.split('hx-post="/abm/save"', 1)[1].split("</form>", 1)[0].count(
        'name="cf_date"') > 0


# --------------------------------------------------------------------------- #
# 6 · VISIBILIDAD en /cashflows. El ancla NO llega al dominio (punto 2) y el router
#     además filtra los montos en cero, así que la visibilidad se resuelve APARTE:
#     el evento de vencimiento se sintetiza desde `inst.maturity_date`.
# --------------------------------------------------------------------------- #

class _RepoStub:
    """Repo con un universo FIJO. Sin esto el test depende del catálogo compartido de
    la suite (que otros tests re-siembran y ensucian) y falla según el orden."""

    def __init__(self, insts):
        self._insts = insts

    def get_all_instruments(self):
        return list(self._insts)


def _cashflows_html(insts) -> str:
    from fastapi.testclient import TestClient

    from apps.web.app import app
    from apps.web.deps import get_repo

    app.dependency_overrides[get_repo] = lambda: _RepoStub(insts)
    try:
        with TestClient(app) as c:
            r = c.get("/cashflows?days=3650")
        assert r.status_code == 200
        return r.text
    finally:
        app.dependency_overrides.pop(get_repo, None)


@pytest.mark.parametrize("itype", ["PURO", "DUAL", "DUAL_CER_TAMAR"])
def test_cashflows_muestra_el_vencimiento_de_los_tipos_analiticos(itype):
    """Los 14 TAMAR/DUAL eran INVISIBLES en /cashflows (0 filas: no tienen schedule y
    la fila ancla ni llega al dominio ni sobreviviría al filtro de montos en cero).
    Ahora aparecen con su vencimiento, montos em-dash y un concepto que lo explica."""
    from datetime import timedelta

    from core.domain.models import Instrument

    vto = date.today() + timedelta(days=400)
    html = _cashflows_html([Instrument(ticker="ZANC1", short_name="Z",
                                       instrument_type=itype, maturity_date=vto)])
    assert ">ZANC1</a>" in html
    fila = html.split(">ZANC1</a>", 1)[1].split("</tr>", 1)[0]
    assert "analítico" in fila                      # el concepto lo aclara
    assert fila.count("—") == 3                     # amort / renta / total en em-dash
    assert vto.strftime("%d/%m/%y") in html


def test_cashflows_sin_vencimiento_no_inventa_una_fila():
    from core.domain.models import Instrument

    html = _cashflows_html([Instrument(ticker="ZANC2", short_name="Z",
                                       instrument_type="PURO")])
    assert "ZANC2" not in html
    assert "Sin flujos en el horizonte" in html


def test_cashflows_sigue_mostrando_los_flujos_reales():
    """Contraprueba: el camino normal (bonos con schedule) no cambia."""
    from datetime import timedelta

    from core.domain.models import Cashflow, Instrument

    vto = date.today() + timedelta(days=300)
    html = _cashflows_html([Instrument(
        ticker="ZREA1", short_name="Z", instrument_type="BONAR", maturity_date=vto,
        cashflows=[Cashflow(date=vto, amortization=100.0, interest=2.5)])])
    assert ">ZREA1</a>" in html
    fila = html.split(">ZREA1</a>", 1)[1].split("</tr>", 1)[0]
    assert "Renta + Amort." in fila
    assert "100.00" in fila and "2.50" in fila and "102.50" in fila


# --------------------------------------------------------------------------- #

def _backfill_mod():
    import sys
    from pathlib import Path
    ruta = str(Path(__file__).resolve().parent.parent / "scripts")
    if ruta not in sys.path:
        sys.path.insert(0, ruta)
    import backfill_tamar_anchor
    return backfill_tamar_anchor


def test_backfill_planifica_solo_los_analiticos_sin_filas(tmp_catalog):
    bf = _backfill_mod()
    _add_bond("BF1", "PURO", [])                              # entra
    _add_bond("BF2", "DUAL_CER_TAMAR", [])                    # entra
    _add_bond("BF3", "PURO", [(_VTO, 0.0, 0.0, True)])        # ya tiene ancla → NO
    _add_bond("BF4", "BONAR", [], sheet="Soberanos")          # no es analítico → NO
    with SessionLocal.begin() as s:
        s.get(InstrumentORM, "BF2").maturity_date = None      # sin vto → no se adivina

    with SessionLocal() as s:
        import sqlalchemy as sa
        rows = s.execute(sa.select(InstrumentORM)).scalars().all()
        assert [e["ticker"] for e in bf.build_plan(rows)] == ["BF1"]
        assert bf.sin_vencimiento(rows) == ["BF2"]


def test_backfill_apply_inserta_el_ancla_y_es_idempotente(tmp_catalog):
    bf = _backfill_mod()
    _add_bond("BF1", "PURO", [])
    assert bf.apply_migration() == 1
    assert _rows("BF1") == [(_VTO, 0.0, 0.0, True)]
    # el dominio sigue viéndolo sin cashflows → el pricing no se movió
    assert CatalogRepository(auto_seed=False).get_instrument_by_ticker("BF1").cashflows == ()
    assert bf.apply_migration() == 0                          # idempotente por CONTENIDO
    assert _rows("BF1") == [(_VTO, 0.0, 0.0, True)]


def test_backfill_dry_run_es_el_default(tmp_catalog):
    """Correrlo sin --apply NO escribe (es la protección de la catalog.db viva)."""
    bf = _backfill_mod()
    _add_bond("BF1", "PURO", [])
    assert bf.main([]) == 0
    assert _rows("BF1") == []


def test_backfill_nunca_borra_filas_existentes(tmp_catalog):
    """Forward-only: un analítico con flujos cargados a mano (dato del usuario) NO se
    toca. Limpiarlos sería una decisión de datos, y este script sólo INSERTA."""
    bf = _backfill_mod()
    _add_bond("BF5", "PURO", [(date(2026, 6, 30), 50.0, 1.0, False)])
    assert bf.apply_migration() == 0
    assert _rows("BF5") == [(date(2026, 6, 30), 50.0, 1.0, False)]


# --------------------------------------------------------------------------- #
# 7 · El RECHAZO tiene que ser VISIBLE. `POST /abm/save` apunta a `#abm-list`, que vive
#     en la vista «Cargados»; el ＋ del tab «Universo BYMA» abre el cajón SIN cambiar de
#     vista, así que ese target queda con `display:none` y el mensaje —que esta ola
#     convirtió en el resultado por defecto del alta— no se veía. El cajón sí está a la
#     vista: el error se espeja ahí con un swap OOB.
# --------------------------------------------------------------------------- #

def _post_save(client, data):
    return client.post("/abm/save", data=data)


def test_el_rechazo_del_save_se_espeja_dentro_del_cajon():
    """Sin esto, dar de alta desde «Universo BYMA» no mostraba NADA: ni ✓, ni error,
    ni cierre del cajón — el operador vuelve a apretar Guardar creyendo que no tomó."""
    from fastapi.testclient import TestClient

    from apps.web.app import app

    with TestClient(app) as c:
        r = _post_save(c, dict(_ON_FIELDS))          # ON sin schedule → rechazo
    assert r.status_code == 200
    assert "FLUJO DE FONDOS" in r.text
    # el espejo OOB existe, apunta al cajón y trae el mensaje
    assert 'id="abm-drawer-err"' in r.text
    assert 'hx-swap-oob' in r.text
    oob = r.text.split('id="abm-drawer-err"', 1)[1].split("</div>", 1)[0]
    assert "abm-err" in oob or "No se guard" in r.text.split('id="abm-drawer-err"', 1)[1][:400]


def test_un_save_exitoso_limpia_el_error_del_cajon():
    """El espejo se vacía en el camino feliz: si no, un error viejo queda pegado en el
    cajón para siempre. Y el `hx-on::after-request` del form decide por la presencia de
    la clase `abm-err`, así que el contenedor vacío NO puede traerla."""
    from fastapi.testclient import TestClient

    from apps.web.app import app

    data = {**_ON_FIELDS,
            "cf_date": ["2027-07-22"], "cf_amort": ["100"], "cf_interest": ["7.5"]}
    with TestClient(app) as c:
        try:
            r = _post_save(c, data)
        finally:
            c.delete("/abm/instrument/W1F0O")     # catálogo compartido: no dejar basura
    assert r.status_code == 200
    assert 'id="abm-drawer-err"' in r.text          # el espejo viaja igual (para limpiar)
    assert "abm-err" not in r.text                  # …pero vacío: el form cierra y flashea


def test_la_pagina_no_duplica_el_id_del_espejo():
    """El bloque OOB sólo sale por la respuesta del POST. Si `abm_list.html` lo
    renderizara también en el include de la página, habría dos `id="abm-drawer-err"`
    y htmx pisaría el equivocado."""
    from fastapi.testclient import TestClient

    from apps.web.app import app

    with TestClient(app) as c:
        html = c.get("/abm").text
    assert html.count('id="abm-drawer-err"') == 1   # el del cajón, y nada más
    assert "hx-swap-oob" not in html


# --------------------------------------------------------------------------- #
# 8 · El PREVIEW tiene que respetar la misma regla que el SAVE. Proponerle un schedule
#     a un tipo de payoff analítico es ofrecerle al operador trabajo que `save_instrument`
#     descarta a propósito — y el ✓ del cajón no distinguía "guardado" de "descartado".
# --------------------------------------------------------------------------- #

def test_preview_no_propone_schedule_a_un_tipo_analitico(tmp_catalog):
    from apps.web import instruments_abm as abm

    campos = dict(_TAMAR_FIELDS, **{"cupon anual %": "2.0", "frecuencia pagos": "2"})
    got = abm.preview_cashflows(campos, "TAMAR")
    assert got["cashflows"] == []
    assert got["nota"]                                   # y dice POR QUÉ está vacía
    assert "cerrada" in got["nota"] or "analítico" in got["nota"]


def test_preview_sigue_proponiendo_para_un_tipo_normal(tmp_catalog):
    """Contraprueba: el synth no se apagó, sólo se acotó."""
    from apps.web import instruments_abm as abm

    got = abm.preview_cashflows(dict(_ON_FIELDS), "Obligaciones_Negociables")
    assert len(got["cashflows"]) == 4                     # 2 años, semestral
    assert not got["nota"]


def test_router_preview_de_un_analitico_devuelve_cero_filas_y_explica():
    from fastapi.testclient import TestClient

    from apps.web.app import app

    campos = dict(_TAMAR_FIELDS, sheet="TAMAR",
                  **{"cupon anual %": "2.0", "frecuencia pagos": "2"})
    with TestClient(app) as c:
        r = c.post("/abm/preview_cashflows", data=campos)
    assert r.status_code == 200
    assert r.text.count('name="cf_date"') == 0           # ni una fila editable
    assert "cerrada" in r.text or "analítico" in r.text  # la nota explica


def test_save_de_un_analitico_no_reporta_flujos_que_descarto(tmp_catalog):
    """`out["cashflows"]` decía 2 habiendo persistido sólo el ancla: el router lo
    loguea y el operador ve ✓ como si se hubieran guardado."""
    from apps.web import instruments_abm as abm

    out = abm.save_instrument("TAMAR", dict(_TAMAR_FIELDS),
                              [{"date": "2026-06-30", "amortization": "50", "interest": "3"},
                               {"date": "2027-06-30", "amortization": "50", "interest": "3"}])
    assert out["cashflows"] == 0                          # lo que REALMENTE se persistió
    assert out.get("descartados") == 2                    # …y lo que se tiró, explícito
    assert _rows("TTX9") == [(_VTO, 0.0, 0.0, True)]


# --------------------------------------------------------------------------- #
# 9 · Los CALLERS del write-path. Sacar el synth de `save_instrument` es un cambio de
#     CONTRATO: todo script que pasaba `cashflows=None` esperando que el store
#     sintetizara ahora revienta sobre una DB donde el bono no existe (un restore, o un
#     droplet nuevo). `load_bond.py` se actualizó en la ola; estos guards evitan que
#     alguno vuelva a quedar atrás.
# --------------------------------------------------------------------------- #

def test_ningun_script_le_pide_el_synth_al_write_path():
    """Guard de clase, no de caso: `cashflows=None` en un script es la firma exacta
    del bug (esperar que el store sintetice). El schedule lo materializa el caller."""
    import re
    from pathlib import Path

    raiz = Path(__file__).resolve().parent.parent / "scripts"
    culpables = []
    for py in sorted(raiz.glob("*.py")):
        txt = py.read_text(encoding="utf-8")
        for m in re.finditer(r"save_instrument\s*\([^)]*cashflows\s*=\s*None", txt):
            culpables.append("%s:%d" % (py.name, txt[:m.start()].count("\n") + 1))
    assert culpables == [], (
        "estos scripts esperan que `save_instrument` sintetice, y ya no lo hace: %s"
        % culpables)


@pytest.mark.parametrize("modulo,ticker", [("ingest_on_ypc4o", "YPC4O"),
                                           ("ingest_on_mcc1o", "MCC1O")])
def test_los_ingest_de_on_materializan_su_schedule(tmp_catalog, modulo, ticker):
    """Sobre una DB VACÍA (el caso que rompía): el schedule que el script imprime es el
    que persiste, y el alta ocurre."""
    import importlib
    import sys
    from pathlib import Path

    ruta = str(Path(__file__).resolve().parent.parent / "scripts")
    if ruta not in sys.path:
        sys.path.insert(0, ruta)
    mod = importlib.import_module(modulo)

    from apps.web import instruments_abm as abm

    synth = abm._safe_synth(mod.FIELDS)
    assert synth, "el synth de %s vino vacío: el test no probaría nada" % ticker
    filas = mod._rows(synth)
    out = abm.save_instrument(mod.SHEET, mod.FIELDS, cashflows=filas)
    assert out["cashflows"] == len(filas)
    guardado = _rows(ticker)
    assert guardado is not None, "%s no quedó cargado" % ticker
    assert [(d, a, i) for d, a, i, _anc in guardado] == [
        (cf.date, cf.amortization, cf.interest) for cf in synth]


# --------------------------------------------------------------------------- #
# 10 · El backfill corre A MANO, en prod, contra la base equivocada con una sola
#      variable de entorno de diferencia: en el droplet `MONITOR_DB_DIR` vive en el
#      drop-in de systemd y un `venv/bin/python scripts/...` desde la shell NO lo ve
#      (resuelve a ~/.local/share/monitor, vacío). Sin estas dos guardas el script
#      imprimía "Nada que anclar" y el operador se iba convencido de que ya estaba.
# --------------------------------------------------------------------------- #

def test_el_backfill_dice_contra_que_base_corre(tmp_catalog, capsys):
    bf = _backfill_mod()
    _add_bond("BF1", "PURO", [])
    assert bf.main([]) == 0
    salida = capsys.readouterr().out
    assert "w1_ancla.db" in salida, "no dice a qué .db le pega:\n%s" % salida


def test_el_backfill_aborta_si_el_catalogo_esta_vacio(tmp_catalog, capsys):
    """Catálogo sin un solo instrumento = casi seguro la DB equivocada. Abortar es
    la única salida honesta: 'nada que anclar' sobre una base vacía es indistinguible
    de 'ya estaba aplicado'."""
    bf = _backfill_mod()
    assert bf.main([]) != 0                      # dry-run tambien aborta
    salida = capsys.readouterr().out
    assert "MONITOR_DB_DIR" in salida             # y dice cuál es la perilla
    assert bf.main(["--apply"]) != 0
