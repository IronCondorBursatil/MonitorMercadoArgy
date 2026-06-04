"""Router del popup de detalle (Fase 4 web). TestClient corre el lifespan;
get_bond_detail tolera snapshot None (sin red) → el test no depende de la red."""

from datetime import date

from fastapi.testclient import TestClient

from apps.web import bond_detail
from apps.web.app import app
from apps.web.routers import bonds as bonds_router
from core.infrastructure.db.catalog_repository import CatalogRepository


def _a_ticker():
    # Primer bono VIVO (no vencido): robusto al orden del catálogo y al paso del
    # tiempo. `[0]` ciego tomaría un vencido si cambia el orden/avanza la fecha.
    repo = CatalogRepository(auto_seed=True)
    settle = bond_detail._resolve_ref(1)
    return next(
        (i.ticker for i in repo.get_all_instruments()
         if i.maturity_date and i.maturity_date > settle and i.cashflows),
        repo.get_all_instruments()[0].ticker,
    )


def _a_cer_ticker():
    # CER VIVO con flujos futuros (ver test_bond_detail._a_cer_ticker): tomar 'el
    # primer CER' agarraba X29Y6, un LECER vencido → el cajón se testeaba contra
    # data muerta (proyección vacía) sin ejercitar realmente los escenarios.
    repo = CatalogRepository(auto_seed=True)
    settle = bond_detail._resolve_ref(1)
    return next(
        (i.ticker for i in repo.get_all_instruments()
         if bond_detail._is_cer_type(i.instrument_type) and i.cer_base
         and i.maturity_date and i.maturity_date > settle
         and i.get_future_cashflows(settle)),
        None,
    )


class _StubRem:
    """REM stub (sin red) para los tests del cajón CER."""

    def get_monthly_path(self):
        return {date(2026, 6, 1): 0.020, date(2026, 7, 1): 0.019, date(2026, 8, 1): 0.018}

    def get_next_12m_yoy(self):
        return 0.25


def test_bond_detail_renders():
    tk = _a_ticker()
    with TestClient(app) as c:
        r = c.get(f"/bond/{tk}/detail")
        assert r.status_code == 200
        for marker in ("Cashflows", "DV01", "Calculadora", "T+1"):
            assert marker in r.text, marker


def test_bond_detail_unknown_404():
    with TestClient(app) as c:
        assert c.get("/bond/NOPE9/detail").status_code == 404


def test_bond_metrics_calculator():
    tk = _a_ticker()
    with TestClient(app) as c:
        r = c.post(f"/bond/{tk}/metrics", data={"settlement_lag": "1", "price": "100"})
        assert r.status_code == 200 and "TIR" in r.text
        r2 = c.post(f"/bond/{tk}/metrics", data={"settlement_lag": "1", "tir_pct": "30"})
        assert r2.status_code == 200 and "Precio dirty" in r2.text


def test_cer_bond_detail_has_projection_button():
    tk = _a_cer_ticker()
    if tk is None:
        return
    with TestClient(app) as c:
        r = c.get(f"/bond/{tk}/detail")
        assert r.status_code == 200
        assert "Proyección CER" in r.text
        assert 'id="cer-drawer-body"' in r.text


def test_cer_drawer_get_renders_months_and_scenarios(monkeypatch):
    tk = _a_cer_ticker()
    if tk is None:
        return
    monkeypatch.setattr(bonds_router, "_rem_provider", lambda: _StubRem())
    with TestClient(app) as c:
        r = c.get(f"/bond/{tk}/cer?lag=1&price=100")
        assert r.status_code == 200
        assert "Valor CER por mes" in r.text
        assert "Retorno por escenario" in r.text
        assert "cer-form" in r.text


def test_cer_drawer_post_custom_mode(monkeypatch):
    import re

    tk = _a_cer_ticker()
    if tk is None:
        return
    monkeypatch.setattr(bonds_router, "_rem_provider", lambda: _StubRem())
    with TestClient(app) as c:
        g = c.get(f"/bond/{tk}/cer?lag=1&price=100")
        m = re.search(r'name="infl_(\d{4}-\d{2})"', g.text)
        assert m, "el cajón no renderizó inputs por mes"
        ym = m.group(1)
        r = c.post(f"/bond/{tk}/cer", data={
            "lag": "1", "mode": "custom", "price": "100", f"infl_{ym}": "3,5",
        })
        assert r.status_code == 200
        assert 'value="3,5"' in r.text   # el valor tipeado se re-inyecta
        assert "editable" in r.text       # modo custom activo
