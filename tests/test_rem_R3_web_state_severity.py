"""Remediación R3_web (lote D2, hallazgo 6) — severidad POR LOOP y enrutado
estructurado de las caídas.

REGRESIÓN QUE INTRODUJO EL FIX ANTERIOR: al retener `last_error` 300s para que un
refresh exitoso no borrara la caída de otro loop, la caída de CUALQUIER loop lateral
(ratings/bei/price_history/options) pasó a pintar el badge del header en ROJO con el
texto "sin datos" —`fragments/header_status.html` sólo mira `st.last_error`— y a
devolver `status: degraded` en `/api/health` durante 5 minutos, con el snapshot de
precios refrescado hace 5 segundos. Antes del fix esa mentira duraba ≤5s; con el fix,
300s. Lo que el hallazgo pedía era distinguir severidad: el refresh loop es crítico,
los otros son degradación PARCIAL.

FRAGILIDAD DEL MISMO FIX: el enrutado al canal `_loop_crashes` dependía de que
`_on_crash` siguiera escribiendo EXACTAMENTE "loop <name> cayó (<motivo>) —
reiniciando" y de que `record_error` no truncara el ')' al cortar a 300 chars.
Ningún test ataba ese contrato: cambiar una palabra lo rompía en silencio.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import apps.web.app as app_mod
from apps.web.app import _crash_reporter, app
from apps.web.state import AppState


def _marcar(state, loop: str, reason: str = "CancelledError espuria") -> None:
    """Cae `loop` y JUSTO DESPUÉS entra un ciclo de refresh sano (el escenario de la
    regresión: precios frescos de hace 5s con un loop lateral caído)."""
    asyncio.run(state.record_loop_crash(loop, reason))
    asyncio.run(state.update([]))


# ── El badge del header: rojo SOLO por el loop de precios ───────────────────
def test_la_caida_de_un_loop_lateral_no_pinta_el_badge_de_rojo():
    with TestClient(app) as c:
        _marcar(app.state.app_state, "ratings")
        r = c.get("/health/badge")
    assert r.status_code == 200
    assert "sin datos" not in r.text, (
        "el scraper de calificaciones se cayó y el header dice 'sin datos' con los "
        "precios refrescados hace 5s")
    # ...pero TAMPOCO puede quedar mudo: el hallazgo pedía ÁMBAR con el nombre del
    # loop. Con el badge verde a secas, la caída de un loop lateral no se ve en
    # NINGUNA superficie de la app (ver test_el_badge_avisa_en_ambar...).
    assert "meta-err" not in r.text, r.text
    assert "loop caído" in r.text and "ratings" in r.text, r.text


def test_el_badge_avisa_en_ambar_cuando_se_cae_un_loop_lateral():
    """Re-auditoría — la mitad de visibilidad del hallazgo 6, que había quedado sin
    hacer: con el badge en verde a secas, la caída de un loop lateral no se veía en
    NINGUNA superficie (header mudo; `loop_crashes` lo publica `status()` pero no lo
    consumía ningún template ni endpoint). El incidente del 2026-09-01 duró 22hs
    justamente por eso."""
    with TestClient(app) as c:
        _marcar(app.state.app_state, "ratings", "HTTPError 503 del scraper FIX")
        r = c.get("/health/badge")
    assert "loop caído: ratings" in r.text, r.text
    assert "meta-stale" in r.text, "tiene que ser ÁMBAR, no rojo ni verde"
    assert "meta-err" not in r.text
    # el motivo va en el tooltip (el badge está detrás de login, a diferencia de
    # /api/health, que lleva sólo nombres)
    assert "503" in r.text, r.text


def test_el_badge_verde_vuelve_cuando_no_hay_nada_caido():
    with TestClient(app) as c:
        asyncio.run(app.state.app_state.update([]))
        r = c.get("/health/badge")
    assert "act " in r.text and "loop caído" not in r.text, r.text


def test_la_caida_del_refresh_loop_si_pinta_el_badge_de_rojo():
    """La contracara: el loop que produce el snapshot SÍ es crítico y su caída tiene
    que sobrevivir al ciclo siguiente (ese era el hallazgo original)."""
    with TestClient(app) as c:
        _marcar(app.state.app_state, "refresh")
        r = c.get("/health/badge")
    assert "sin datos" in r.text, "la caída del loop de precios no llegó al badge"
    assert "refresh" in r.text


# ── /api/health: `status` habla de los precios; el resto se reporta aparte ──
def test_api_health_no_degrada_por_un_loop_lateral_pero_lo_reporta():
    with TestClient(app) as c:
        _marcar(app.state.app_state, "ratings")
        body = c.get("/api/health").json()
    assert body["status"] == "ok", (
        "un loop lateral caído degrada el health del monitor entero: los probes "
        "externos alertan por precios que están perfectos")
    assert body["is_stale"] is False
    assert body["degraded_loops"] == ["ratings"], body


def test_api_health_degrada_cuando_cae_el_refresh_loop():
    with TestClient(app) as c:
        _marcar(app.state.app_state, "refresh")
        body = c.get("/api/health").json()
    assert body["status"] == "degraded"
    assert body["degraded_loops"] == ["refresh"]


def test_el_motivo_crudo_de_la_caida_no_sale_por_el_endpoint_publico():
    """`/api/health` es público: `degraded_loops` lleva NOMBRES, no el string de la
    excepción (que arrastra URLs/params internos de los providers)."""
    with TestClient(app) as c:
        _marcar(app.state.app_state, "price_history",
                "HTTPError: https://interno/secreto?token=abc123")
        raw = c.get("/api/health").text
    assert "token=abc123" not in raw and "interno" not in raw, raw


# ── El nombre del loop llega ESTRUCTURADO (no parseando el mensaje) ─────────
def _reportar(loop: str, reason: str) -> AppState:
    """Corre el `on_crash` REAL de app.py contra un AppState propio."""
    state = AppState()
    fake_app = SimpleNamespace(state=SimpleNamespace(app_state=state))
    asyncio.run(_crash_reporter(fake_app)(loop, reason))
    return state


def test_un_motivo_largo_no_pierde_la_caida():
    """`record_error` trunca a 300 chars: con el enrutado por regex, un `reason`
    largo dejaba el ')' fuera del corte y la caída no llegaba al canal por loop."""
    reason = "ValueError: " + "x" * 400
    state = _reportar("price_history", reason)
    assert [c["loop"] for c in state.loop_crashes()] == ["price_history"], \
        "la caída se perdió al parsear el mensaje"


def test_un_motivo_con_parentesis_y_saltos_de_linea_llega_intacto():
    """El regex `\\((.*)\\)` cortaba en el último ')' de la PRIMERA línea."""
    reason = "RuntimeError: falló el fetch (timeout de 40s)\ny además el breaker abrió"
    state = _reportar("bei", reason)
    crashes = state.loop_crashes()
    assert [c["loop"] for c in crashes] == ["bei"]
    assert crashes[0]["reason"] == reason, "el motivo llegó recortado por el parseo"


def test_el_reporter_no_pinta_de_rojo_un_loop_lateral():
    state = _reportar("options", "boom")
    assert state.last_error is None
    assert state.status(stale_after_s=30)["degraded_loops"] == ["options"]


def test_un_motivo_largo_tampoco_pierde_la_RETENCION_del_badge():
    """Re-auditoría: el mismo defecto del truncado, en la otra mitad del camino.

    `record_loop_crash` arma "loop refresh cayó (<motivo>) — reiniciando" y lo corta a
    300 chars, así que con un motivo de más de 281 (típico: `f"{type(e).__name__}: {e}"`
    de un ValidationError/StatementError) el ')' queda fuera del corte. Mientras
    `_keep_error` deducía la severidad parseando ESE mensaje, el primer refresh exitoso
    (5s después, apenas el supervisor reinicia el loop) borraba el badge rojo — o sea
    el bug original del hallazgo 6, vivo justo para el único loop crítico.
    """
    async def run():
        st = AppState()
        await st.record_loop_crash("refresh", "HTTPStatusError: " + "x" * 400)
        await st.update([])                       # el loop reinició y el ciclo anduvo
        s = st.status(stale_after_s=30)
        assert s["last_error"] and "refresh" in s["last_error"], (
            "la caída del loop de precios se borró al primer refresh: con el motivo "
            "largo el mensaje pierde el ')' y dejaba de reconocerse como caída")
        assert s["ok"] is False
        assert s["degraded_loops"] == ["refresh"]
    asyncio.run(run())


def test_un_motivo_largo_de_un_loop_lateral_sigue_sin_pintar_el_badge():
    """La contracara del anterior: el largo del motivo no puede convertir una caída
    lateral en crítica (la severidad la da el NOMBRE del loop, nada más)."""
    async def run():
        st = AppState()
        await st.record_loop_crash("ratings", "HTTPStatusError: " + "x" * 400)
        await st.update([])
        s = st.status(stale_after_s=30)
        assert s["last_error"] is None and s["ok"] is True
        assert s["degraded_loops"] == ["ratings"]
    asyncio.run(run())


def test_el_reporter_si_marca_el_loop_critico():
    state = _reportar("refresh", "boom")
    assert state.last_error and "refresh" in state.last_error


# ── Wiring vivo: el lifespan usa ese reporter (no un string armado a mano) ──
@pytest.mark.noauth
def test_el_lifespan_encamina_la_caida_de_un_loop_lateral_al_canal_por_loop(monkeypatch):
    """End-to-end del contrato supervisor → app.py → AppState: un loop lateral que
    muere queda en `loop_crashes` CON su nombre, y NO apaga el semáforo."""
    monkeypatch.delenv("MONITOR_DISABLE_LOOPS", raising=False)

    async def _noop(app):
        return None

    async def _dormido(app):
        await asyncio.sleep(3600)

    async def _muere(app):
        raise RuntimeError("scraper FIX caído (503)")

    monkeypatch.setattr(app_mod, "_startup_reconcile", _noop)
    monkeypatch.setattr(app_mod, "_ratings_loop", _muere)
    for nombre in ("_refresh_loop", "_options_loop", "_bei_loop", "_price_history_loop"):
        monkeypatch.setattr(app_mod, nombre, _dormido)

    visto = {}

    async def _correr():
        async with app_mod.lifespan(app_mod.app):
            await asyncio.sleep(0.2)
            st = app_mod.app.state.app_state
            visto["crashes"] = st.loop_crashes()
            visto["last_error"] = st.last_error

    asyncio.run(_correr())

    assert [c["loop"] for c in visto["crashes"]] == ["ratings"], visto["crashes"]
    assert "503" in visto["crashes"][0]["reason"], visto["crashes"][0]
    assert visto["last_error"] is None, (
        "la caída del loop de ratings apagó el semáforo de los precios: %s"
        % visto["last_error"])
