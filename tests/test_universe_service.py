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
    """`tipo_especie` distinto del default del bucket (`"Obligaciones Negociables"` para
    `corp`, ver `_BUCKET_META`): así el assert de categoría sólo pasa si la ficha de
    verdad viaja hasta la fila del catálogo Y hasta la novedad — si se borrara
    `fila["categoria"] = campos["categoria"]` en el servicio, la categoría de la fila
    seguiría siendo el default del bucket y este test lo detectaría."""
    _seed_catalogo(60)

    def ficha(symbol):
        assert symbol == "YMCXO"
        return {"isin": "ARYPFS000001", "emisor": "YPF S.A.", "denominacion": "ON YPF CL X",
                "tipo_especie": "Oblig. Negociables PYMES", "vencimiento": "2029-06-30"}

    res = svc.sincronizar_universo(_hub({"YMCXO": "corp"}), hoy=HOY, ficha_fn=ficha)
    assert res.fichas == 1
    f = _fila("YMCXO")
    assert f.isin == "ARYPFS000001" and f.emisor == "YPF S.A."
    assert f.vencimiento == "2029-06-30" and f.denominacion == "ON YPF CL X"
    assert f.categoria == "Oblig. Negociables PYMES"
    assert _nov("YMCXO").categoria == "Oblig. Negociables PYMES"


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


def test_una_pendiente_sin_isin_recibe_la_ficha_en_la_corrida_siguiente(base):
    """«El resto, mañana»: un símbolo nuevo que se quedó sin ficha (tope o falla) no
    puede perderla para siempre sólo porque ya entró a `byma_catalog` — mientras la
    novedad siga `nueva` y su fila siga sin ISIN, la corrida siguiente la reintenta."""
    _seed_catalogo(60)
    svc.sincronizar_universo(_hub({"YMCXO": "corp"}), hoy=HOY, ficha_fn=_sin_ficha,
                             max_fichas=0)
    assert _fila("YMCXO").isin is None

    pedidas = []

    def ficha(symbol):
        pedidas.append(symbol)
        return {"isin": "ARYPFS000001", "emisor": "YPF S.A.", "denominacion": "ON YPF CL X",
                "tipo_especie": "Oblig. Negociables PYMES", "vencimiento": "2029-06-30"}

    res = svc.sincronizar_universo(_hub({"YMCXO": "corp"}), hoy=HOY, ficha_fn=ficha)
    assert pedidas == ["YMCXO"]                    # ni K001 (sin ISIN pero no es novedad)
    f = _fila("YMCXO")
    assert f.isin == "ARYPFS000001" and f.emisor == "YPF S.A."
    assert f.denominacion == "ON YPF CL X" and f.vencimiento == "2029-06-30"
    assert f.categoria == "Oblig. Negociables PYMES"
    assert _nov("YMCXO").categoria == "Oblig. Negociables PYMES"
    assert res.fichas == 1


def test_una_pendiente_con_isin_no_se_reintenta(base):
    """Si la fila del catálogo ya tiene ISIN (lo trajo una corrida anterior o el CSV),
    no es candidata a reintento aunque la novedad siga `nueva`."""
    _seed_catalogo(60)
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="S29E7", categoria="Títulos Públicos",
                             isin="ARARGS100017"))
        nov.registrar_nuevas_en(s, [{"symbol": "S29E7", "source": "byma",
                                     "categoria": "Títulos Públicos"}], hoy=HOY)
    pedidas = []

    def ficha(symbol):
        pedidas.append(symbol)
        return None

    svc.sincronizar_universo(_hub({}), hoy=HOY, ficha_fn=ficha)
    assert pedidas == []


def test_la_ficha_no_se_gasta_en_categorias_sin_hoja_y_prioriza_titulos_publicos(base):
    """El tope de fichas es el recurso escaso de la corrida (una POST BYMA por símbolo).
    Para acciones/cedears/índices la ficha responde `data: []` SIEMPRE y encima ordenan
    primero por alfabético: el tope se agotaba ahí y los títulos públicos y las ON —lo
    único que el ABM puede cargar— se quedaban sin metadata corrida tras corrida."""
    _seed_catalogo(60)
    pedidas = []

    def ficha(symbol):
        pedidas.append(symbol)
        return None

    svc.sincronizar_universo(_hub({"AAPLD": "cedears", "YMCXO": "corp", "S29E7": "notes"}),
                             hoy=HOY, ficha_fn=ficha, max_fichas=2)
    assert pedidas == ["S29E7", "YMCXO"]        # letra/título público antes que la ON
    assert "AAPLD" not in pedidas
    assert _fila("AAPLD") is not None           # pero el cedear igual entra al universo


def test_una_pendiente_sin_hoja_no_se_reintenta(base):
    """El reintento se acota igual: una acción pendiente (que nunca va a tener ficha) se
    llevaba un lugar del tope en TODAS las corridas siguientes."""
    _seed_catalogo(60)
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="GGAL2", categoria="Acciones"))
        s.add(BymaCatalogORM(symbol="YMCXO", categoria="Obligaciones Negociables"))
        nov.registrar_nuevas_en(s, [
            {"symbol": "GGAL2", "source": "byma", "categoria": "Acciones"},
            {"symbol": "YMCXO", "source": "byma", "categoria": "Obligaciones Negociables"},
        ], hoy=HOY)
    pedidas = []

    def ficha(symbol):
        pedidas.append(symbol)
        return None

    svc.sincronizar_universo(_hub({}), hoy=HOY, ficha_fn=ficha)
    assert pedidas == ["YMCXO"]


def test_una_pendiente_sin_fila_en_el_catalogo_no_gasta_ficha(base):
    """Con el outer join una novedad sin fila en `byma_catalog` (isin NULL por la
    extensión del join) entraba a los candidatos, gastaba una ficha y después se
    descartaba sola: no hay fila donde escribir el resultado."""
    _seed_catalogo(60)
    with SessionLocal.begin() as s:
        nov.registrar_nuevas_en(s, [{"symbol": "HUERFANA", "source": "byma",
                                     "categoria": "Obligaciones Negociables"}], hoy=HOY)
    pedidas = []

    def ficha(symbol):
        pedidas.append(symbol)
        return {"isin": "ARFANTASMA01"}

    svc.sincronizar_universo(_hub({}), hoy=HOY, ficha_fn=ficha)
    assert pedidas == []


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


# ── siembra del universo: sólo si está vacía, en el lifespan, nunca por debajo del job ─
def test_seed_byma_universe_siembra_solo_la_tabla_vacia(base, monkeypatch):
    """`ingest_byma_catalog` es DELETE+INSERT: correrla sobre una tabla poblada borraba lo
    que el job diario agrega (S29E7, last_seen). Ahora sólo siembra la tabla VACÍA."""
    from apps.web import app as app_mod
    from core.infrastructure.byma import universe
    llamadas = []
    monkeypatch.setattr(universe, "ingest_byma_catalog",
                        lambda *a, **k: llamadas.append(1) or 7)

    assert app_mod._seed_byma_universe() == 7          # vacía → siembra
    assert llamadas == [1]
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="S29E7", categoria="Títulos Públicos", last_seen="2026-09-07"))
    assert app_mod._seed_byma_universe() == 0          # poblada → ni la toca
    assert llamadas == [1]
    assert _fila("S29E7").last_seen == "2026-09-07"


def test_startup_reconcile_ya_no_siembra_el_universo(base, monkeypatch):
    """La siembra era el 6º paso de `_startup_reconcile` (después de minutos de fichas, en
    paralelo con el primer diff del job). Ahora vive en el lifespan: reconciliar no toca
    `ingest_byma_catalog` ni `_seed_byma_universe`, y llega hasta el final."""
    import asyncio

    from apps.web import app as app_mod
    from apps.web.state import AppState
    from core.infrastructure.byma import catalog_enrich, universe
    llamadas = []
    monkeypatch.setattr(universe, "ingest_byma_catalog",
                        lambda *a, **k: llamadas.append("ingest") or 0)
    monkeypatch.setattr(app_mod, "_seed_byma_universe", lambda: llamadas.append("seed") or 0)
    monkeypatch.setattr(app_mod, "_reconcile_catalog", lambda hub: 0)
    monkeypatch.setattr(app_mod, "_backfill_legs", lambda: llamadas.append("legs") or 0)
    for fn in ("enrich_isin_from_byma", "enrich_isin_from_ficha", "enrich_ficha_meta"):
        monkeypatch.setattr(catalog_enrich, fn, lambda *a, **k: 0)
    monkeypatch.setattr(app_mod, "get_repo", lambda: SimpleNamespace(
        type_health={"orphans": [], "defaulted": []}, seed_error=None,
        get_all_instruments=lambda: [], reload=lambda: None))

    class _HubBoot:
        async def refresh_all(self):
            return {}

    fake_app = SimpleNamespace(state=SimpleNamespace(hub=_HubBoot(), app_state=AppState()))
    asyncio.run(app_mod._startup_reconcile(fake_app))
    assert llamadas == ["legs"], llamadas      # llegó al final SIN sembrar


def _stub_loops_para_siembra(monkeypatch, evento):
    """Loops y reconcile por no-ops; la siembra avisa por `evento`."""
    from apps.web import app as app_mod

    async def _noop(app):
        return None

    for nombre in ("_startup_reconcile", "_refresh_loop", "_options_loop", "_bei_loop",
                   "_price_history_loop", "_ratings_loop", "_universe_loop"):
        monkeypatch.setattr(app_mod, nombre, _noop)
    monkeypatch.setattr(app_mod, "_seed_byma_universe", lambda: evento.set() or 0)


def test_el_lifespan_siembra_el_universo_antes_de_cualquier_loop(monkeypatch):
    """Sin `MONITOR_DISABLE_LOOPS` el lifespan siembra (best-effort) ANTES de crear las
    tasks; con la variable puesta (pytest) no toca nada."""
    from fastapi.testclient import TestClient

    from apps.web import app as app_mod
    evento = threading.Event()
    _stub_loops_para_siembra(monkeypatch, evento)
    monkeypatch.delenv("MONITOR_DISABLE_LOOPS", raising=False)
    with TestClient(app_mod.app):
        assert evento.wait(5.0), "el lifespan no llamó a _seed_byma_universe"

    evento.clear()
    monkeypatch.setenv("MONITOR_DISABLE_LOOPS", "1")
    with TestClient(app_mod.app):
        assert not evento.wait(0.3)
