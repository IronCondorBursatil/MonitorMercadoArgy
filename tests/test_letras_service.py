"""El lado que TOCA la base de la sincronización de letras.

El planificador es puro y se testea en `test_letras_sync.py`. Acá se prueba lo otro:
que la foto del catálogo sea la que el planificador espera, que el alta escriba de
verdad por el camino de la ABM (con sus guards) y —sobre todo— que sin `aplicar=True`
no se escriba **nada**. Esto le agrega filas a la fuente de verdad desde una API de
terceros: que el default sea inocuo no es un detalle de comodidad.
"""

from datetime import date

import pytest

from apps.web.letras_service import foto_del_catalogo, sincronizar
from config.settings import settings
from core.infrastructure.db import engine as db_engine
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import CashflowORM, InstrumentORM

HOY = date(2026, 9, 4)


@pytest.fixture
def base(tmp_path):
    """Engine sobre una .db temporal VACÍA (sin sembrar del Excel: acá interesa el
    camino de alta, no el universo entero)."""
    db_engine.configure(tmp_path / "letras_test.db")
    init_db()
    try:
        yield
    finally:
        db_engine.configure(settings.catalog_db)


def _api(ticker="S30X6", emision="2026-01-30", vto="2026-12-30", tem=2.5, vpv=117.5):
    return {"ticker": ticker, "fechaEmision": emision, "fechaVencimiento": vto,
            "tem": tem, "vpv": vpv}


def _letras_en_base():
    with SessionLocal() as s:
        return sorted(i.ticker for i in s.query(InstrumentORM)
                      .filter(InstrumentORM.instrument_type.in_(("LECAP", "BONCAP"))))


# ── el default no escribe ────────────────────────────────────────────────────
def test_sin_aplicar_NO_escribe_nada(base):
    """El modo por default —el que corre el loop y el script sin `--apply`— tiene
    que ser inocuo. Si esto se rompe, una API de terceros escribe sola en la fuente
    de verdad."""
    plan = sincronizar(payload=[_api()], hoy=HOY)
    assert len(plan.altas) == 1, "el plan tiene que VER el alta..."
    assert plan.aplicadas == [], "...pero no haberla hecho"
    assert _letras_en_base() == []


# ── el alta, de punta a punta ────────────────────────────────────────────────
def test_con_aplicar_la_letra_queda_en_la_base_con_su_flujo(base):
    plan = sincronizar(payload=[_api("S30X6", vpv=117.5)], aplicar=True, hoy=HOY)
    assert plan.aplicadas == ["S30X6"]
    assert _letras_en_base() == ["S30X6"]

    with SessionLocal() as s:
        orm = s.query(InstrumentORM).filter_by(ticker="S30X6").one()
        assert orm.instrument_type == "LECAP"
        assert str(orm.maturity_date) == "2026-12-30"
        assert str(orm.emission_date) == "2026-01-30"
        cfs = s.query(CashflowORM).filter_by(ticker="S30X6").all()

    assert len(cfs) == 1, "una letra capitalizable paga TODO junto al vencimiento"
    assert str(cfs[0].fecha_pago) == "2026-12-30"
    assert (cfs[0].amortizacion or 0) + (cfs[0].cupon_interes or 0) == pytest.approx(117.5)
    assert not cfs[0].es_ancla, "una letra no tiene payoff analítico: es un flujo REAL"


def test_la_letra_dada_de_alta_APARECE_para_el_motor(base):
    """El alta no sirve de nada si el bono queda invisible: todo el read-path filtra
    por igualdad exacta de `instrument_type`. Es el bug que ya costó 44 bonos."""
    from core.domain.instrument_groups import is_known_type

    sincronizar(payload=[_api("T30X7", vto="2027-12-30")], aplicar=True, hoy=HOY)
    with SessionLocal() as s:
        tipo = s.query(InstrumentORM).filter_by(ticker="T30X7").one().instrument_type
    assert tipo == "BONCAP"
    assert is_known_type(tipo)


def test_correr_dos_veces_no_duplica_ni_pisa(base):
    """Idempotencia: el loop lo va a correr todos los días sobre el mismo payload."""
    sincronizar(payload=[_api()], aplicar=True, hoy=HOY)
    plan = sincronizar(payload=[_api()], aplicar=True, hoy=HOY)
    assert plan.altas == [] and plan.sin_cambios == 1
    assert _letras_en_base() == ["S30X6"]
    with SessionLocal() as s:
        assert s.query(CashflowORM).filter_by(ticker="S30X6").count() == 1


def test_una_letra_que_no_entra_no_frena_a_las_demas(base):
    """Cada alta es independiente. Con un ticker roto en el medio, las buenas tienen
    que entrar igual: si no, una fila mala de la API bloquea la sincronización entera
    hasta que alguien la mire."""
    payload = [_api("S30X6"), _api("ZZZZZ"), _api("T30X7", vto="2027-12-30")]
    plan = sincronizar(payload=payload, aplicar=True, hoy=HOY)
    assert sorted(plan.aplicadas) == ["S30X6", "T30X7"]
    assert [i["ticker"] for i in plan.invalidas] == ["ZZZZZ"]
    assert _letras_en_base() == ["S30X6", "T30X7"]


# ── la foto que consume el planificador ──────────────────────────────────────
def test_la_foto_trae_el_pago_TOTAL_del_schedule(base):
    """El planificador compara ese total contra el `vpv`, que es el pago final. Si la
    foto trajera sólo el primer flujo, cualquier letra con más de una fila daría una
    diferencia fantasma en cada corrida."""
    sincronizar(payload=[_api(vpv=117.5)], aplicar=True, hoy=HOY)
    with SessionLocal.begin() as s:
        s.add(CashflowORM(ticker="S30X6", fecha_pago=date(2026, 6, 30),
                          amortizacion=2.5, cupon_interes=0.0))
    foto = foto_del_catalogo()
    assert foto["S30X6"]["pago"] == pytest.approx(120.0)


def test_la_foto_ignora_una_fila_ancla(base):
    """El ancla (`es_ancla=1`) no es un pago: es la fila que deja auditable un bono
    de payoff analítico. Sumarla daría un total falso."""
    sincronizar(payload=[_api(vpv=117.5)], aplicar=True, hoy=HOY)
    with SessionLocal.begin() as s:
        s.add(CashflowORM(ticker="S30X6", fecha_pago=date(2026, 12, 30),
                          amortizacion=0.0, cupon_interes=0.0, es_ancla=1))
    assert foto_del_catalogo()["S30X6"]["pago"] == pytest.approx(117.5)


def test_la_foto_solo_mira_letras(base):
    """`BONOFIJA` también es TASA_FIJA pero NO es capitalizable (TO26 y TY30P pagan
    cupones) y la API no lo lista: meterlo en la foto lo haría figurar eternamente
    como 'está en el catálogo y la API no lo trae'."""
    sincronizar(payload=[_api()], aplicar=True, hoy=HOY)
    with SessionLocal.begin() as s:
        s.add(InstrumentORM(ticker="TO26", short_name="TO26",
                            instrument_type="BONOFIJA", maturity_date=date(2026, 10, 17)))
    assert list(foto_del_catalogo()) == ["S30X6"]


# ── el guard del payload, en el camino real ──────────────────────────────────
def test_un_payload_roto_no_llega_a_escribir(base):
    """El guard del planificador tiene que cortar ANTES de la escritura, no después."""
    # Sembrar en UNA sola corrida: cargando de a una, el propio guard se dispara a
    # mitad del setup (1 letra en el payload contra 5 ya en la base).
    sincronizar(payload=[_api("S%02dX6" % i) for i in range(1, 12)],
                aplicar=True, hoy=HOY)
    antes = _letras_en_base()
    assert len(antes) == 11

    plan = sincronizar(payload=[_api("S99X6")], aplicar=True, hoy=HOY)
    assert plan.rechazado is not None
    assert plan.aplicadas == []
    assert _letras_en_base() == antes, "escribió a pesar de haber rechazado el payload"


# ── el cableado ──────────────────────────────────────────────────────────────
def test_la_sincronizacion_esta_CABLEADA_al_loop_diario():
    """El motivo de este test: `argentinadatos_provider.fetch_letras()` ya pegaba a
    este mismo endpoint desde hacía tiempo y **no lo llamaba nadie**. Código que
    funciona, pasa el gate y no hace nada. Un módulo nuevo sin consumidor repite
    exactamente eso."""
    import inspect

    from apps.web.app import _price_history_loop

    fuente = inspect.getsource(_price_history_loop)
    assert "letras_service" in fuente, "el loop no llama a la sincronización de letras"
    assert "settings.letras_autosync" in fuente, "el loop ignora la perilla"


def test_el_alta_automatica_va_DESPUES_del_backup_del_dia():
    """Orden deliberado: es la única escritura automática sobre la fuente de verdad,
    así que toda alta queda precedida por una copia del día."""
    import inspect

    from apps.web.app import _price_history_loop

    fuente = inspect.getsource(_price_history_loop)
    assert fuente.index("backup_db") < fuente.index("letras_service")


def test_la_perilla_apagada_deja_el_loop_MIRANDO(base):
    """Apagar el autosync no puede apagar el aviso: el operador tiene que enterarse
    igual de que hay letras nuevas sin cargar."""
    plan = sincronizar(payload=[_api()], aplicar=False, hoy=HOY)
    assert len(plan.altas) == 1 and plan.aplicadas == []
    assert "1 alta" in plan.resumen()
