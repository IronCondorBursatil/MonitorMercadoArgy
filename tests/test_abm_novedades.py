"""Pestaña Novedades del ABM: fragment agrupado, Cargar prefillado, Descartar/Restaurar,
Refrescar (admin) y la transición nueva→cargada al guardar (spec 2026-09-07 §3)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from apps.web import app as app_mod
from apps.web import universe_service as svc
from apps.web.app import app
from core.infrastructure.byma import novedades as nov
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import BymaCatalogORM, UniverseNovedadORM
from tests._clock import ref_date
from tests.test_abm_router import _con_preview

HOY = ref_date()
_SYMS = ("TSNV1O", "TSNVGG")


def _sembrar():
    """Dos novedades en el catálogo COMPARTIDO del sandbox: una ON (cargable) y una
    acción (sin hoja). Se limpian en `_limpiar`."""
    init_db()
    with SessionLocal.begin() as s:
        s.merge(BymaCatalogORM(symbol="TSNV1O", ticker_pesos="TSNV1O", moneda="ARS",
                               clase_liquidacion="primary", cotiza=1,
                               categoria="Obligaciones Negociables", emisor="NOVEDAD S.A.",
                               isin="ARNOVE000001", vencimiento="2028-01-31"))
        s.merge(BymaCatalogORM(symbol="TSNVGG", ticker_pesos="TSNVGG", moneda="ARS",
                               clase_liquidacion="primary", cotiza=1, categoria="Acciones"))
        nov.registrar_nuevas_en(s, [
            {"symbol": "TSNV1O", "source": "byma", "categoria": "Obligaciones Negociables"},
            {"symbol": "TSNVGG", "source": "data912", "categoria": "Acciones"},
        ], hoy=HOY)


def _limpiar():
    with SessionLocal.begin() as s:
        s.execute(delete(UniverseNovedadORM).where(UniverseNovedadORM.symbol.in_(_SYMS)))
        s.execute(delete(BymaCatalogORM).where(BymaCatalogORM.symbol.in_(_SYMS)))


@pytest.fixture
def novedades():
    _sembrar()
    try:
        yield
    finally:
        _limpiar()


def test_el_fragment_agrupa_por_categoria_y_solo_ofrece_cargar_donde_hay_hoja(novedades):
    with TestClient(app) as c:
        r = c.get("/abm/novedades")
    assert r.status_code == 200
    html = r.text
    assert "Obligaciones Negociables" in html and "Acciones" in html
    assert "TSNV1O" in html and "TSNVGG" in html and "NOVEDAD S.A." in html
    assert 'hx-get="/abm/form?prefill=TSNV1O"' in html          # ＋ Cargar prefillado
    assert 'hx-get="/abm/form?prefill=TSNVGG"' not in html      # acción: sin hoja → sin ＋
    assert 'hx-post="/abm/novedades/TSNVGG/descartar"' in html  # pero sí Descartar
    assert "2028-01-31" in html                                  # vencimiento de la ficha


def test_descartar_y_restaurar_actualizan_estado_y_contador(novedades):
    with TestClient(app) as c:
        st = app_mod.app.state.app_state
        r = c.post("/abm/novedades/TSNVGG/descartar")
        assert r.status_code == 200 and "restaurar" in r.text.lower()
        assert "TSNVGG" in [f["symbol"] for f in nov.listar("descartada")]
        assert st.novedades() == nov.contar_nuevas()
        r = c.post("/abm/novedades/TSNVGG/restaurar")
        assert r.status_code == 200
        assert "TSNVGG" not in [f["symbol"] for f in nov.listar("descartada")]


def test_la_pagina_del_abm_trae_la_pestana_novedades_primera(novedades):
    with TestClient(app) as c:
        page = c.get("/abm").text
    assert 'id="view-novedades"' in page and 'data-v="novedades"' in page
    assert page.index('data-v="novedades"') < page.index('data-v="cargados"')
    assert 'hx-get="/abm/novedades"' in page
    assert 'class="abm-seg"' in page and "Universo BYMA" in page and 'id="abm-list"' in page


def test_un_alta_desde_el_abm_marca_la_novedad_como_cargada(novedades):
    fields = {
        "sheet": "Obligaciones_Negociables",
        "ticker_ars": "TSNV1O", "ticker_mep": "", "ticker_ccl": "",
        "short_name": "NOVEDAD S.A.", "tipo": "HARD DOLLAR", "ley_aplicable": "Argentina",
        "fecha_emision": "2026-01-31", "fecha_vencimiento": "2028-01-31",
        "cupon anual %": "8", "frecuencia pagos": "2",
        "base calculo": "ACT/365", "tipo amortizacion": "bullet",
    }
    with TestClient(app) as c:
        try:
            r = c.post("/abm/save", data=_con_preview(fields))
            assert r.status_code == 200 and "No se guardó" not in r.text
            assert r.headers.get("HX-Trigger") == "novedades-refresh"
            with SessionLocal() as s:
                assert s.get(UniverseNovedadORM, "TSNV1O").estado == "cargada"
            assert app_mod.app.state.app_state.novedades() == nov.contar_nuevas()
        finally:
            c.delete("/abm/instrument/TSNV1O")


def test_refrescar_ahora_corre_la_sincronizacion_y_muestra_el_resumen(novedades, monkeypatch):
    llamadas = []

    def _sync(hub, *, hoy):
        llamadas.append(hoy)
        return svc.Resultado(vistos=1200, pendientes=2)

    monkeypatch.setattr(svc, "sincronizar_universo", _sync)
    with TestClient(app) as c:
        r = c.post("/abm/novedades/refresh")
    assert r.status_code == 200 and len(llamadas) == 1
    assert "1200 vistos" in r.text


# ── el refresh exige admin (auth real) ──────────────────────────────────────
@pytest.mark.noauth
def test_refrescar_ahora_exige_admin():
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
        # bob TIENE la pestaña abm: así el 403 viene del check de admin, no del de pestaña
        s.add(UserORM(username="bob", hashed_password=get_password_hash("bobpass"),
                      is_admin=False, allowed_tabs=["abm"]))
        s.commit()
    try:
        with TestClient(app) as c:
            c.post("/login", data={"username": "bob", "password": "bobpass"})
            assert c.get("/abm/novedades", follow_redirects=False).status_code == 200
            r = c.post("/abm/novedades/refresh", follow_redirects=False)
            assert r.status_code == 403
        with TestClient(app) as c:
            r = c.post("/abm/novedades/refresh",
                       headers={"Origin": "http://evil.example"}, follow_redirects=False)
            assert r.status_code == 403                      # CSRF antes que todo
    finally:
        with SessionLocal() as s:
            s.query(UserORM).delete()
            s.commit()
        auth_router._login_attempts.clear()
