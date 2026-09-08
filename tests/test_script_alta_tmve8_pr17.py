"""`scripts/alta_tmve8_pr17.py`: dry-run por default, altas idempotentes, PR17 con su schedule
(13 cuotas que suman el capital ajustado) y TMVE8 por el borde del ABM (ancla + fx_base)."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import alta_tmve8_pr17 as alta  # noqa: E402


@pytest.fixture
def tmp_catalog(tmp_path):
    from config.settings import settings
    from core.infrastructure.db import engine as db_engine
    from core.infrastructure.db.catalog_repository import init_db

    db_engine.configure(tmp_path / "alta.db")
    init_db()
    try:
        yield
    finally:
        db_engine.configure(settings.catalog_db)


def test_el_schedule_de_pr17_tiene_13_cuotas_trimestrales_que_suman_el_capital():
    cfs = alta.pr17_cashflows()
    assert len(cfs) == 13
    assert cfs[0][0] == date(2026, 5, 2) and cfs[-1][0] == date(2029, 5, 2)
    assert sum(a for _, a, _ in cfs) == pytest.approx(alta.PR17_CAPITAL_AJUSTADO, abs=1e-4)
    assert alta.PR17_CAPITAL_AJUSTADO == pytest.approx(705.70982004 / 0.86)
    # 10 del 7 %, 2 del 9 %, 1 del 12 %; interés decreciente con el saldo
    amorts = [a for _, a, _ in cfs]
    assert amorts[:10] == [amorts[0]] * 10 and amorts[10] == amorts[11] > amorts[0] < amorts[12]
    intereses = [i for _, _, i in cfs]
    assert all(x > 0 for x in intereses) and intereses[0] > intereses[-1]


def test_dry_run_no_escribe(tmp_catalog, capsys):
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM

    assert alta.main([]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    with SessionLocal() as s:
        assert s.get(InstrumentORM, "TMVE8") is None and s.get(InstrumentORM, "PR17") is None


def test_apply_da_de_alta_los_dos_y_es_idempotente(tmp_catalog):
    from core.infrastructure.db.catalog_repository import CatalogRepository
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import CashflowORM, InstrumentORM

    assert alta.apply_altas() == {"TMVE8": "created", "PR17": "created"}
    repo = CatalogRepository(auto_seed=False)
    t = repo.get_instrument_by_ticker("TMVE8")
    assert t is not None and t.instrument_type == "DUAL_DL_TAMAR"
    assert t.fx_base == pytest.approx(1499.8387) and t.cashflows == ()      # ancla filtrada
    p = repo.get_instrument_by_ticker("PR17")
    assert p is not None and p.instrument_type == "PROVINCIAL ARS"
    assert len(p.cashflows) == 13 and p.day_count == "ACT/365" and p.payment_frequency == 4
    with SessionLocal() as s:
        assert s.get(InstrumentORM, "PR17").sheet == "Provinciales"
        assert s.query(CashflowORM).filter_by(ticker="TMVE8").count() == 1   # sólo el ancla
    # segunda corrida: TMVE8 se re-guarda (upsert), PR17 no se duplica
    assert alta.apply_altas() == {"TMVE8": "updated", "PR17": "skipped (ya existe)"}
    with SessionLocal() as s:
        assert s.query(CashflowORM).filter_by(ticker="PR17").count() == 13


def test_main_apply_pasa_por_el_guard(tmp_catalog, monkeypatch):
    monkeypatch.setattr(alta, "guard_write", lambda tag, force=False: 2)
    assert alta.main(["--apply"]) == 2
