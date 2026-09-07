"""Borde del job de novedades: lee el hub, escribe byma_catalog + universe_novedades en
una transacción, nunca instruments (spec 2026-09-07 §2)."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apps.web import universe_service as svc
from core.infrastructure.byma import novedades as nov
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal, configure
from core.infrastructure.db.models import BymaCatalogORM, InstrumentORM, UniverseNovedadORM
from tests._clock import ref_date

HOY = ref_date()


@pytest.fixture
def base(tmp_path):
    from config.settings import settings
    configure(tmp_path / "svc.db")
    init_db()
    try:
        yield
    finally:
        configure(settings.catalog_db)


class _Hub:
    """Doble del ProviderHub: snapshot/sources/freshness/active_mode con la forma real."""

    def __init__(self, vistos: dict, frescos=(), active_mode="byma_open"):
        self._vistos = dict(vistos)
        self._frescos = set(frescos)
        self.active_mode = active_mode

    def snapshot(self, settle="24"):
        return {s: SimpleNamespace(c=100.0, v=None, q_op=None) for s in self._vistos}

    def sources(self):
        return dict(self._vistos)

    def freshness(self):
        return {s: 1.0 for s in self._frescos}


def _seed_catalogo(n: int) -> None:
    with SessionLocal.begin() as s:
        s.add_all([BymaCatalogORM(symbol=f"K{i:03d}", ticker_pesos=f"K{i:03d}", moneda="ARS",
                                  categoria="Acciones", clase_liquidacion="primary", cotiza=1)
                   for i in range(n)])


def _hub(extra: dict, frescos=(), n=60) -> _Hub:
    vistos = {f"K{i:03d}": "stocks" for i in range(n)}
    vistos.update(extra)
    return _Hub(vistos, frescos)


def _fila(symbol):
    with SessionLocal() as s:
        return s.get(BymaCatalogORM, symbol)


def _nov(symbol):
    with SessionLocal() as s:
        return s.get(UniverseNovedadORM, symbol)


def _sin_ficha(_symbol):
    return None


def test_una_especie_nueva_entra_al_catalogo_y_a_novedades(base):
    _seed_catalogo(60)
    res = svc.sincronizar_universo(_hub({"S29E7": "notes"}, frescos={"S29E7"}),
                                   hoy=HOY, ficha_fn=_sin_ficha)
    assert res.rechazo is None
    assert res.nuevas == ["S29E7"] and res.altas_catalogo == 1 and res.pendientes == 1
    f = _fila("S29E7")
    assert f.panel == "Letras" and f.categoria == "Títulos Públicos"
    assert f.last_seen == HOY.isoformat() and f.moneda == "ARS"
    n = _nov("S29E7")
    assert n.estado == "nueva" and n.source == "byma" and n.first_seen == HOY.isoformat()
    assert _fila("K001").last_seen == HOY.isoformat()      # lo visto se marca


def test_con_data912_activa_la_procedencia_no_dice_byma(base):
    """`freshness()` lista lo que trajo la ACTIVA, sea cual sea: con Data912 activa todo
    vino de Data912 aunque esté 'fresco'."""
    _seed_catalogo(60)
    hub = _hub({"S29E7": "notes"}, frescos={"S29E7"})
    hub.active_mode = "data912"
    svc.sincronizar_universo(hub, hoy=HOY, ficha_fn=_sin_ficha)
    assert _nov("S29E7").source == "data912"


def test_lo_que_desaparece_del_feed_no_se_borra_ni_se_toca(base):
    _seed_catalogo(60)
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="ZZZ1", categoria="Obligaciones Negociables"))
    svc.sincronizar_universo(_hub({}), hoy=HOY, ficha_fn=_sin_ficha)
    f = _fila("ZZZ1")
    assert f is not None and f.last_seen is None


def test_un_simbolo_ya_cargado_en_instruments_no_es_novedad(base):
    _seed_catalogo(60)
    with SessionLocal.begin() as s:
        s.merge(InstrumentORM(ticker="TSTX1O", ticker_mep="TSTX1D"))
    res = svc.sincronizar_universo(_hub({"TSTX1D": "corp"}), hoy=HOY, ficha_fn=_sin_ficha)
    assert res.nuevas == [] and res.altas_catalogo == 1
    assert _fila("TSTX1D") is not None and _nov("TSTX1D") is None


def test_una_pendiente_que_se_cargo_pasa_a_cargada(base):
    _seed_catalogo(60)
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="S29E7", categoria="Títulos Públicos"))
        nov.registrar_nuevas_en(s, [{"symbol": "S29E7", "source": "byma",
                                     "categoria": "Títulos Públicos"}], hoy=HOY)
        s.merge(InstrumentORM(ticker="S29E7"))
    res = svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY, ficha_fn=_sin_ficha)
    assert res.cargadas == ["S29E7"] and res.pendientes == 0
    assert _nov("S29E7").estado == "cargada"


def test_una_descartada_no_vuelve(base):
    _seed_catalogo(60)
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="ZZZ1", categoria="Obligaciones Negociables"))
        nov.registrar_nuevas_en(s, [{"symbol": "ZZZ1", "source": "byma",
                                     "categoria": "Obligaciones Negociables"}], hoy=HOY)
    nov.descartar("ZZZ1")
    res = svc.sincronizar_universo(_hub({"ZZZ1": "corp"}), hoy=HOY, ficha_fn=_sin_ficha)
    assert res.nuevas == [] and _nov("ZZZ1").estado == "descartada"


def test_hub_vacio_rechaza_y_no_toca_nada_ni_sella_el_dia(base):
    _seed_catalogo(60)
    res = svc.sincronizar_universo(_Hub({}), hoy=HOY, ficha_fn=_sin_ficha)
    assert res.rechazo and res.nuevas == []
    assert _fila("K001").last_seen is None
    assert svc.ultima_corrida() is None


def test_sin_universo_sembrado_no_hay_diff(base):
    """Sin línea de base (byma_catalog vacío: la siembra no corrió o falló) una corrida
    marcaría TODO el feed como novedad y dejaría la siembra en no-op para siempre."""
    from core.infrastructure.byma import universe
    res = svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY, ficha_fn=_sin_ficha)
    assert res.rechazo and "vacío" in res.rechazo
    assert res.altas_catalogo == 0 and _fila("S29E7") is None and _nov("S29E7") is None
    assert svc.ultima_corrida() is None and universe.count() == 0


def test_lectura_parcial_contra_la_corrida_anterior_se_rechaza(base):
    _seed_catalogo(60)
    with SessionLocal.begin() as s:
        nov.escribir_meta_en(s, svc.META_ULTIMOS_VISTOS, 1000)
    res = svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY, ficha_fn=_sin_ficha)
    assert res.rechazo and "corte parcial" in res.rechazo
    assert _nov("S29E7") is None


def test_la_corrida_buena_sella_el_dia_y_los_vistos(base):
    _seed_catalogo(60)
    svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY, ficha_fn=_sin_ficha)
    assert svc.ultima_corrida() == HOY.isoformat()
    assert nov.leer_meta(svc.META_ULTIMOS_VISTOS) == "61"


def test_la_ficha_enriquece_isin_emisor_vencimiento_y_categoria(base):
    _seed_catalogo(60)

    def ficha(symbol):
        assert symbol == "YMCXO"
        return {"isin": "ARYPFS000001", "emisor": "YPF S.A.", "denominacion": "ON YPF CL X",
                "tipo_especie": "Obligaciones Negociables", "vencimiento": "2029-06-30"}

    res = svc.sincronizar_universo(_hub({"YMCXO": "corp"}), hoy=HOY, ficha_fn=ficha)
    assert res.fichas == 1
    f = _fila("YMCXO")
    assert f.isin == "ARYPFS000001" and f.emisor == "YPF S.A."
    assert f.vencimiento == "2029-06-30" and f.denominacion == "ON YPF CL X"
    assert f.categoria == "Obligaciones Negociables"
    assert _nov("YMCXO").categoria == "Obligaciones Negociables"


def test_una_ficha_rota_no_frena_la_corrida(base):
    _seed_catalogo(60)

    def ficha(_symbol):
        raise RuntimeError("BYMA 503")

    res = svc.sincronizar_universo(_hub({"YMCXO": "corp"}), hoy=HOY, ficha_fn=ficha)
    assert res.nuevas == ["YMCXO"] and res.fichas == 0
    assert _fila("YMCXO").isin is None


def test_la_ficha_se_pide_solo_para_lo_nuevo_y_con_tope(base):
    _seed_catalogo(60)
    pedidas = []

    def ficha(symbol):
        pedidas.append(symbol)
        return None

    extra = {f"N{i:02d}O": "corp" for i in range(5)}
    svc.sincronizar_universo(_hub(extra), hoy=HOY, ficha_fn=ficha, max_fichas=3)
    assert len(pedidas) == 3 and all(p.startswith("N") for p in pedidas)


def test_una_corrida_que_revienta_a_mitad_no_deja_nada_a_medias(base, monkeypatch):
    """Spec §5: diff+upsert son UNA transacción. `escribir_meta_en` es la última escritura
    del bloque: si revienta, catálogo, last_seen y novedades tienen que volver atrás."""
    _seed_catalogo(60)

    def boom(s, key, value):
        raise RuntimeError("fallo simulado a mitad de la corrida")

    monkeypatch.setattr(nov, "escribir_meta_en", boom)
    with pytest.raises(RuntimeError):
        svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY, ficha_fn=_sin_ficha)
    assert _fila("S29E7") is None                 # el alta al catálogo se deshizo
    assert _nov("S29E7") is None                  # la novedad también
    assert _fila("K001").last_seen is None        # y el last_seen de lo visto
    assert svc.ultima_corrida() is None


def test_dos_corridas_solapadas_no_se_pisan(base):
    """El loop de las 08:00 y «Refrescar ahora» corren en hilos distintos: las dos leerían
    el mismo `catalogo` y la segunda moriría insertando el mismo PK. Una por vez."""
    _seed_catalogo(60)
    adentro, soltar = threading.Event(), threading.Event()

    def ficha_lenta(_symbol):
        adentro.set()
        soltar.wait(5)
        return None

    out = {}

    def primera():
        out["r1"] = svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY,
                                             ficha_fn=ficha_lenta)

    t = threading.Thread(target=primera)
    t.start()
    try:
        assert adentro.wait(5)
        r2 = svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY, ficha_fn=_sin_ficha)
    finally:
        soltar.set()
        t.join(5)
    assert r2.rechazo and "en curso" in r2.rechazo
    assert out["r1"].nuevas == ["S29E7"] and _fila("S29E7") is not None


def test_jamas_escribe_instruments(base):
    _seed_catalogo(60)
    svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY, ficha_fn=_sin_ficha)
    with SessionLocal() as s:
        assert s.execute(select(InstrumentORM)).scalars().all() == []
