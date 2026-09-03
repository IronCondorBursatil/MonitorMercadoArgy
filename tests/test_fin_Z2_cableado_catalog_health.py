"""Lote Z2_cableado — las señales del catálogo, CABLEADAS.

Tres agujeros del mismo patrón (la señal se construye y nadie la consume, que es
lo que dejó el bug original invisible durante meses):

1. `CatalogRepository.type_health` no tenía NINGÚN consumidor en la app: sus únicos
   lectores eran los tests. Las filas con `instrument_type` huérfano —cargadas, con
   cashflows y precio, pero invisibles en TODOS los paneles— podían volver a serlo
   en silencio. Ahora el arranque las publica en `AppState` → `/api/health`.

2. "que el fallo de carga del repo llegue a `AppState.record_error`" nunca se hizo.
   Ahora sí: la siembra fallida (badge + señal sticky) y el `reload()` que revienta
   después del reconcile de arranque.

3. CAMBIO DE CONTRATO DE BOOTSTRAP SIN FLAGEAR. `get_all_with_meta()` pasó a LANZAR
   cuando el Excel semilla no aporta nada (correcto: antes se sembraba un catálogo
   vacío en silencio), pero ese `raise` subía por `CatalogRepository.__init__` →
   `get_repo()` → lifespan, y con la DB vacía + semilla ilegible la app **ya no
   arrancaba**: se perdían hasta `/login` y `/api/health`, o sea la superficie donde
   se lee el motivo. Decisión: el `raise` se queda donde corresponde (el camino de
   SIEMBRA nunca debe sembrar 0 filas mudo) y el constructor lo ATRAPA, publica el
   motivo y levanta degradado. Ver `CatalogRepository._seed`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import apps.web.app as app_mod
from apps.web.app import _publish_catalog_health, _startup_reconcile, app
from apps.web.state import AppState


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _fake_repo(*, orphans=(), defaulted=(), seed_error=None, instruments=3):
    """Repo mínimo con la superficie que lee el publicador de salud."""
    return SimpleNamespace(
        type_health={"orphans": [{"ticker": t} for t in orphans],
                     "defaulted": [{"ticker": t} for t in defaulted]},
        seed_error=seed_error,
        get_all_instruments=lambda: [object()] * instruments,
        reload=lambda: None,
    )


def _seed_xlsx(tmp_path, name="master.xlsx"):
    """Excel semilla mínimo pero VÁLIDO (una fila TAMAR + su cashflow)."""
    p = tmp_path / name
    with pd.ExcelWriter(p) as w:
        pd.DataFrame([{"ticker": "TTM26", "tipo": "PURO", "fecha_emision": "2025-01-31",
                       "fecha_vencimiento": "2026-12-31", "spread": 0.05}]
                     ).to_excel(w, sheet_name="TAMAR", index=False)
        pd.DataFrame([{"ticker": "TTM26", "fecha_pago": "2026-12-31",
                       "amortizacion": 100.0, "cupon_interes": 0.0}]
                     ).to_excel(w, sheet_name="Cashflows", index=False)
    return str(p)


# --------------------------------------------------------------------------- #
# (1) type_health llega a AppState y a /api/health
# --------------------------------------------------------------------------- #
def test_el_arranque_publica_la_salud_del_catalogo_en_appstate(monkeypatch):
    """El lifespan tiene que publicar `type_health` apenas warmea el repo. Sin este
    cableado el reporte existía y no lo leía NADIE en la app."""
    monkeypatch.setattr(app_mod, "get_repo",
                        lambda: _fake_repo(orphans=["BF39O"], defaulted=["YMCIO"]))
    with TestClient(app):
        health = app.state.app_state.catalog_health()
    assert health["orphans"] == ["BF39O"], health
    assert health["defaulted"] == ["YMCIO"], health
    assert health["instruments"] == 3
    assert health["at"] is not None


def test_api_health_reporta_las_filas_invisibles(monkeypatch):
    """La superficie que mira un operador/probe: `/api/health` cuenta los huérfanos."""
    monkeypatch.setattr(app_mod, "get_repo",
                        lambda: _fake_repo(orphans=["BF39O", "BF39D"], defaulted=["YMCIO"]))
    with TestClient(app) as c:
        body = c.get("/api/health").json()
    assert body["catalog"]["orphans"] == 2, body
    assert body["catalog"]["defaulted"] == 1, body
    assert body["catalog"]["seed_failed"] is False


def test_los_huerfanos_no_degradan_el_semaforo_de_precios(monkeypatch):
    """Condición CRÓNICA (hoy hay decenas de filas así): si pintara el semáforo,
    quedaría en rojo permanente y nadie volvería a mirarlo. Se reporta aparte."""
    monkeypatch.setattr(app_mod, "get_repo", lambda: _fake_repo(orphans=["BF39O"]))
    with TestClient(app):
        state = app.state.app_state
        asyncio.run(state.update([]))          # un ciclo de precios sano
        st = state.status()
    assert st["ok"] is True, st
    assert st["last_error"] is None
    assert st["catalog"]["orphans"] == 1


def test_el_endpoint_publico_no_filtra_tickers_ni_el_motivo_del_fallo(monkeypatch):
    """`/api/health` es público: el bloque `catalog` lleva CUENTAS y un booleano, no
    el inventario de tickers ni el string crudo de la excepción (paths del server)."""
    monkeypatch.setattr(app_mod, "get_repo", lambda: _fake_repo(
        orphans=["BF39O"], seed_error="RuntimeError: C:/interno/secreto/master.xlsx"))
    with TestClient(app) as c:
        raw = c.get("/api/health").text
    assert "BF39O" not in raw, raw
    assert "secreto" not in raw and "master.xlsx" not in raw, raw
    assert '"seed_failed":true' in raw.replace(" ", ""), raw


# --------------------------------------------------------------------------- #
# (2) El fallo de carga del repo llega a AppState.record_error
# --------------------------------------------------------------------------- #
def test_el_fallo_de_siembra_llega_a_record_error(monkeypatch):
    monkeypatch.setattr(app_mod, "get_repo", lambda: _fake_repo(
        seed_error="RuntimeError: el Excel semilla no aportó ningún instrumento",
        instruments=0))
    with TestClient(app):
        err = app.state.app_state.last_error
    assert err and "siembra" in err, err


def test_el_catalogo_vacio_se_ve_en_el_badge_y_no_lo_borra_un_refresh(monkeypatch):
    """Sticky a propósito: un ciclo de refresh 'exitoso' de 0 instrumentos no arregla
    un catálogo que no se pudo sembrar, así que no puede volver el badge a verde."""
    monkeypatch.setattr(app_mod, "get_repo", lambda: _fake_repo(
        seed_error="RuntimeError: semilla ilegible", instruments=0))
    with TestClient(app) as c:
        asyncio.run(app.state.app_state.update([]))    # el refresh que antes lo borraba
        badge = c.get("/health/badge").text
        body = c.get("/api/health").json()
    assert "sin datos" in badge, badge
    assert body["status"] == "degraded", body
    assert body["catalog"]["seed_failed"] is True


def test_el_reload_que_revienta_en_el_reconcile_llega_a_record_error(monkeypatch):
    """`_startup_reconcile` envolvía TODO en un `except` que sólo loguea: si el
    reload del catálogo fallaba, el cache quedaba viejo y no se veía en ningún lado."""
    from core.infrastructure.byma import catalog_enrich, universe

    def _boom():
        raise RuntimeError("db bloqueada")

    repo = _fake_repo()
    repo.reload = _boom
    monkeypatch.setattr(app_mod, "get_repo", lambda: repo)
    monkeypatch.setattr(app_mod, "_reconcile_catalog", lambda hub: 1)
    monkeypatch.setattr(app_mod, "_backfill_legs", lambda: 0)
    monkeypatch.setattr(catalog_enrich, "enrich_isin_from_byma", lambda: 0)
    monkeypatch.setattr(catalog_enrich, "enrich_isin_from_ficha", lambda: 0)
    monkeypatch.setattr(catalog_enrich, "enrich_ficha_meta", lambda: 0)
    monkeypatch.setattr(universe, "ingest_byma_catalog", lambda: 0)

    class _Hub:
        async def refresh_all(self):
            return None

    state = AppState()
    fake_app = SimpleNamespace(state=SimpleNamespace(hub=_Hub(), app_state=state))
    asyncio.run(_startup_reconcile(fake_app))

    assert state.last_error and "reload" in state.last_error, state.last_error
    # ...y aun así publica la salud del cache que SÍ se está sirviendo.
    assert state.catalog_health()["at"] is not None


def test_el_reconcile_republica_la_salud_del_catalogo(monkeypatch):
    """El reconcile da de alta filas (acciones, ONs, patas) → puede traer tipos
    huérfanos nuevos; la señal tiene que quedar actualizada, no la del boot."""
    from core.infrastructure.byma import catalog_enrich, universe

    monkeypatch.setattr(app_mod, "get_repo", lambda: _fake_repo(orphans=["NUEVO"]))
    monkeypatch.setattr(app_mod, "_reconcile_catalog", lambda hub: 1)
    monkeypatch.setattr(app_mod, "_backfill_legs", lambda: 0)
    monkeypatch.setattr(catalog_enrich, "enrich_isin_from_byma", lambda: 0)
    monkeypatch.setattr(catalog_enrich, "enrich_isin_from_ficha", lambda: 0)
    monkeypatch.setattr(catalog_enrich, "enrich_ficha_meta", lambda: 0)
    monkeypatch.setattr(universe, "ingest_byma_catalog", lambda: 0)

    class _Hub:
        async def refresh_all(self):
            return None

    state = AppState()
    fake_app = SimpleNamespace(state=SimpleNamespace(hub=_Hub(), app_state=state))
    asyncio.run(_startup_reconcile(fake_app))
    assert state.catalog_health()["orphans"] == ["NUEVO"]


def test_publicar_la_salud_es_idempotente_y_refleja_el_ultimo_reporte():
    """Republicar con el catálogo ya sano apaga la señal (no se acumula)."""
    state = AppState()
    fake_app = SimpleNamespace(state=SimpleNamespace(app_state=state))
    asyncio.run(_publish_catalog_health(fake_app, _fake_repo(orphans=["BF39O"])))
    assert state.catalog_status()["orphans"] == 1
    asyncio.run(_publish_catalog_health(fake_app, _fake_repo()))
    assert state.catalog_status() == {"instruments": 3, "orphans": 0, "defaulted": 0,
                                      "seed_failed": False}


# --------------------------------------------------------------------------- #
# (3) Contrato de bootstrap: el raise se queda en la SIEMBRA, no en el arranque
# --------------------------------------------------------------------------- #
def test_una_semilla_ilegible_no_mata_el_arranque_pero_deja_el_motivo(tmp_db, caplog):
    """DB vacía + semilla ausente: el repo se CONSTRUYE (la app levanta y se puede
    leer el diagnóstico), con 0 instrumentos y el motivo publicado."""
    from core.infrastructure.db.catalog_repository import CatalogRepository

    with caplog.at_level("ERROR"):
        repo = CatalogRepository(xlsx_path=str(tmp_db / "no-existe.xlsx"))

    assert repo.get_all_instruments() == []
    assert repo.seed_error and "no-existe.xlsx" in repo.seed_error
    assert any("VACÍO" in r.getMessage() for r in caplog.records), caplog.text


def test_el_camino_de_siembra_sigue_lanzando_con_una_semilla_ilegible(tmp_db):
    """La otra mitad del contrato, intacta: `ingest_from_excel` NUNCA siembra 0 filas
    en silencio (el guard anti-pérdida de `reseed_with_meta` no puede disparar con la
    DB vacía, que es justo el bootstrap de un droplet nuevo)."""
    from core.infrastructure.db.catalog_repository import ingest_from_excel

    with pytest.raises(RuntimeError, match="(?i)semilla"):
        ingest_from_excel(str(tmp_db / "no-existe.xlsx"))


def test_una_semilla_valida_siembra_y_no_deja_error(tmp_db):
    from core.infrastructure.db.catalog_repository import CatalogRepository

    repo = CatalogRepository(xlsx_path=_seed_xlsx(tmp_db))
    assert repo.seed_error is None
    assert [i.ticker for i in repo.get_all_instruments()] == ["TTM26"]


def test_con_la_db_poblada_no_se_siembra_ni_se_mira_el_excel(tmp_db):
    """El Excel es SEMILLA de bootstrap: con la DB ya poblada ni se abre, así que una
    semilla rota no puede degradar un catálogo sano."""
    from core.infrastructure.db.catalog_repository import CatalogRepository

    CatalogRepository(xlsx_path=_seed_xlsx(tmp_db))                 # puebla la DB
    repo = CatalogRepository(xlsx_path=str(tmp_db / "no-existe.xlsx"))
    assert repo.seed_error is None
    assert [i.ticker for i in repo.get_all_instruments()] == ["TTM26"]


def test_el_repo_sembrado_a_medias_publica_seed_failed(tmp_db):
    """Extremo a extremo del item 3: el repo degradado, publicado, sale por health."""
    from core.infrastructure.db.catalog_repository import CatalogRepository

    repo = CatalogRepository(xlsx_path=str(tmp_db / "no-existe.xlsx"))
    state = AppState()
    fake_app = SimpleNamespace(state=SimpleNamespace(app_state=state))
    asyncio.run(_publish_catalog_health(fake_app, repo))
    assert state.catalog_status() == {"instruments": 0, "orphans": 0, "defaulted": 0,
                                      "seed_failed": True}
    assert state.status()["ok"] is False
