"""`_universe_loop`: wiring bajo el supervisor, siembra antes que el loop, apagado bajo
pytest, publica el contador, reintenta tras rechazo y no muere por una corrida rota
(spec 2026-09-07 §2, §4 y §6)."""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from apps.web import app as app_mod
from apps.web import universe_service as svc
from apps.web.state import AppState
from config.settings import settings
from core.infrastructure.byma import novedades as nov
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import UniverseNovedadORM
from tests._clock import ref_date


async def _esperar(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


async def _cancelar(task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _stub_loops(monkeypatch, evento: threading.Event, nombres=None):
    """Todo el lifespan por no-ops; el loop bajo test avisa por `evento` y anota el
    nombre de su task (lo pone `supervise`)."""
    async def _noop(app):
        return None

    async def _spy(app):
        if nombres is not None:
            nombres.append(asyncio.current_task().get_name())
        evento.set()
        await asyncio.sleep(3600)

    for nombre in ("_startup_reconcile", "_refresh_loop", "_options_loop", "_bei_loop",
                   "_price_history_loop", "_ratings_loop"):
        monkeypatch.setattr(app_mod, nombre, _noop)
    monkeypatch.setattr(app_mod, "_universe_loop", _spy)
    monkeypatch.setattr(app_mod, "_seed_byma_universe", lambda: 0)   # no ingerir el CSV real


def test_lifespan_registra_el_universe_loop_bajo_el_supervisor(monkeypatch):
    evento, nombres = threading.Event(), []
    _stub_loops(monkeypatch, evento, nombres)
    monkeypatch.delenv("MONITOR_DISABLE_LOOPS", raising=False)
    with TestClient(app_mod.app):
        assert evento.wait(5.0), "el lifespan no arrancó _universe_loop"
    assert nombres == ["loop:universe"], "el loop no corre envuelto en supervise()"


def test_la_siembra_del_universo_corre_antes_que_el_loop(monkeypatch):
    """La línea de base del diff (byma_catalog) tiene que existir ANTES de que el job
    pueda correr; si la siembra vuelve a `_startup_reconcile`, el primer diff marca todo
    el feed como novedad y anula la siembra para siempre."""
    orden, evento = [], threading.Event()
    _stub_loops(monkeypatch, evento)
    monkeypatch.setattr(app_mod, "_seed_byma_universe", lambda: orden.append("seed") or 0)

    async def _spy(app):
        orden.append("loop")
        evento.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(app_mod, "_universe_loop", _spy)
    monkeypatch.delenv("MONITOR_DISABLE_LOOPS", raising=False)
    with TestClient(app_mod.app):
        assert evento.wait(5.0)
    assert orden[:2] == ["seed", "loop"], orden


def test_disable_loops_no_arranca_el_universe_loop(monkeypatch):
    evento = threading.Event()
    _stub_loops(monkeypatch, evento)
    monkeypatch.setenv("MONITOR_DISABLE_LOOPS", "1")
    with TestClient(app_mod.app):
        assert not evento.wait(0.3)


def _fake_app():
    return SimpleNamespace(state=SimpleNamespace(hub=object(), app_state=AppState()))


def _hoy_ar() -> str:
    """La MISMA expresión que usa el loop para «hoy» (no `date.today()`)."""
    return datetime.now(ZoneInfo(settings.timezone)).date().isoformat()


def _cablear(monkeypatch, *, pendientes_iniciales=0, resultado=None, error=None):
    """Dobles de todo lo que el loop toca: contador persistido, agenda, sello, corrida.
    El doble NO sella el día cuando devuelve rechazo (como el servicio real)."""
    llamadas = {"sync": 0}
    hecho = {"fecha": None}

    def _sync(hub, *, hoy):
        llamadas["sync"] += 1
        if error:
            raise error
        res = resultado or svc.Resultado(pendientes=3)
        if not res.rechazo:
            hecho["fecha"] = hoy.isoformat()
        return res

    monkeypatch.setattr(nov, "contar_nuevas", lambda: pendientes_iniciales)
    monkeypatch.setattr(nov, "proximo_despertar",
                        lambda now, *, hecha_hoy: 3600.0 if hecha_hoy else 0.0)
    monkeypatch.setattr(svc, "ultima_corrida", lambda: hecho["fecha"])
    monkeypatch.setattr(svc, "sincronizar_universo", _sync)
    return llamadas


def test_publica_el_contador_persistido_al_arrancar_sin_sincronizar(monkeypatch):
    """El badge no espera a las 08:00: lo que quedó pendiente ayer se ve al reiniciar. Y
    con el día ya sellado NO corre la sincronización."""
    llamadas = _cablear(monkeypatch, pendientes_iniciales=7)
    monkeypatch.setattr(svc, "ultima_corrida", _hoy_ar)      # ya corrió hoy
    app = _fake_app()

    async def run():
        task = asyncio.create_task(app_mod._universe_loop(app))
        try:
            assert await _esperar(lambda: app.state.app_state.novedades() == 7)
            await asyncio.sleep(0.05)
            assert llamadas["sync"] == 0, "sincronizó con el día ya sellado"
        finally:
            await _cancelar(task)

    asyncio.run(run())


def test_corre_una_vez_y_publica_las_pendientes(monkeypatch):
    llamadas = _cablear(monkeypatch, resultado=svc.Resultado(pendientes=2))
    app = _fake_app()

    async def run():
        task = asyncio.create_task(app_mod._universe_loop(app))
        try:
            assert await _esperar(lambda: llamadas["sync"] == 1)
            assert await _esperar(lambda: app.state.app_state.novedades() == 2)
            await asyncio.sleep(0.05)
            assert llamadas["sync"] == 1, "volvió a correr con el día ya sellado"
            assert not task.done()
        finally:
            await _cancelar(task)

    asyncio.run(run())


def test_una_corrida_rechazada_espera_el_tick_y_reintenta(monkeypatch):
    """Sin el `sleep` del `if res.rechazo`, `ultima_corrida()` sigue None,
    `proximo_despertar` devuelve 0 y el loop martilla SQLite y el hub sin parar."""
    llamadas = _cablear(monkeypatch,
                        resultado=svc.Resultado(rechazo="lectura anémica: 10 símbolos (< 50)",
                                                pendientes=1))
    monkeypatch.setattr(app_mod, "_UNIVERSE_REINTENTO_SEC", 1.0)
    app = _fake_app()

    async def run():
        task = asyncio.create_task(app_mod._universe_loop(app))
        try:
            assert await _esperar(lambda: llamadas["sync"] == 1)
            assert await _esperar(lambda: app.state.app_state.novedades() == 1)  # publica lo pendiente igual
            await asyncio.sleep(0.1)
            assert llamadas["sync"] == 1, "reintentó sin esperar el tick (busy loop)"
            assert not task.done(), "el rechazo terminó el loop"
            assert await _esperar(lambda: llamadas["sync"] >= 2, timeout=3.0), "no reintentó"
        finally:
            await _cancelar(task)

    asyncio.run(run())


def test_una_corrida_rota_no_tumba_el_loop_y_espera_el_tick(monkeypatch):
    llamadas = _cablear(monkeypatch, error=RuntimeError("SQLite locked"))
    monkeypatch.setattr(app_mod, "_UNIVERSE_REINTENTO_SEC", 1.0)
    app = _fake_app()

    async def run():
        task = asyncio.create_task(app_mod._universe_loop(app))
        try:
            assert await _esperar(lambda: llamadas["sync"] == 1)
            await asyncio.sleep(0.1)
            assert llamadas["sync"] == 1, "reintentó sin esperar el tick tras la excepción"
            assert not task.done(), "la excepción de la corrida tumbó el loop"
            assert await _esperar(lambda: llamadas["sync"] >= 2, timeout=3.0), \
                "no reintentó tras la excepción"
        finally:
            await _cancelar(task)

    asyncio.run(run())


def test_status_y_health_llevan_el_contador_sin_tickers():
    """`/api/health` es público: va la CUENTA, jamás un símbolo (patrón de
    test_fin_Z2 / test_rem_R3: meter un dato distintivo y asertar que no sale)."""
    state = AppState()
    state.set_novedades(4)
    assert state.status()["novedades"] == 4
    with TestClient(app_mod.app) as c:
        init_db()
        try:
            with SessionLocal.begin() as s:
                nov.registrar_nuevas_en(s, [{"symbol": "TSNVHLTH", "source": "byma",
                                             "categoria": "Acciones"}], hoy=ref_date())
            app_mod.app.state.app_state.set_novedades(nov.contar_nuevas())
            r = c.get("/api/health")
            assert r.json()["novedades"] >= 1
            assert "TSNVHLTH" not in r.text
        finally:
            with SessionLocal.begin() as s:
                s.execute(delete(UniverseNovedadORM)
                          .where(UniverseNovedadORM.symbol == "TSNVHLTH"))


def test_el_badge_del_header_linkea_al_abm_solo_si_hay_novedades():
    with TestClient(app_mod.app) as c:
        app_mod.app.state.app_state.set_novedades(0)
        assert "novedad" not in c.get("/health/badge").text
        app_mod.app.state.app_state.set_novedades(2)
        html = c.get("/health/badge").text
    assert "2 novedades" in html and 'href="/abm"' in html and "meta-nov" in html
    # lo de siempre sigue ahí (bajo test nunca hubo refresh → 'datos viejos' o 'sin datos')
    assert ("datos viejos" in html) or ("sin datos" in html)


@pytest.mark.noauth
def test_el_badge_de_novedades_respeta_el_permiso_de_la_pestana_abm():
    """`has_tab("abm")` es la mitad del guard que el bypass de conftest no puede probar:
    un usuario sin la pestaña ABM no ve el link aunque haya novedades."""
    from apps.web.routers import auth as auth_router
    from core.infrastructure.db.engine import get_engine
    from core.infrastructure.db.models import Base, UserORM
    from core.security import get_password_hash

    Base.metadata.create_all(bind=get_engine())
    auth_router._login_attempts.clear()
    with SessionLocal() as s:
        s.query(UserORM).delete()
        s.add(UserORM(username="admin", hashed_password=get_password_hash("adminpass"),
                      is_admin=True, allowed_tabs=["*"]))
        s.add(UserORM(username="bob", hashed_password=get_password_hash("bobpass"),
                      is_admin=False, allowed_tabs=["bonos"]))
        s.commit()
    try:
        with TestClient(app_mod.app) as c:
            c.post("/login", data={"username": "bob", "password": "bobpass"})
            app_mod.app.state.app_state.set_novedades(2)
            r = c.get("/health/badge", follow_redirects=False)
            assert r.status_code == 200
            assert "novedad" not in r.text and "meta-nov" not in r.text
        with TestClient(app_mod.app) as c:
            c.post("/login", data={"username": "admin", "password": "adminpass"})
            app_mod.app.state.app_state.set_novedades(2)
            assert "2 novedades" in c.get("/health/badge", follow_redirects=False).text
    finally:
        with SessionLocal() as s:
            s.query(UserORM).delete()
            s.commit()
        auth_router._login_attempts.clear()
