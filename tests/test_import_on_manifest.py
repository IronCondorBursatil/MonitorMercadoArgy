"""Altas autorizadas nunca pisan catálogo previo ni escriben sin respaldo."""
import copy

import pytest

from scripts import import_on_manifest as loader


@pytest.fixture
def manifest():
    return {
        "schema_version": 1, "batch_id": "on-test", "source_sha256": "a" * 64,
        "records": [{
            "fields": {
                "ticker_ars": "NEW1O", "short_name": "Emisor de prueba",
                "tipo": "HARD DOLLAR", "fecha_emision": "2026-01-15",
                "fecha_vencimiento": "2027-01-15", "cupon anual %": 6.0,
                "frecuencia pagos": 2, "base calculo": "30/360",
                "ley_aplicable": "Argentina", "sector_override": "Servicios Financieros",
            },
            "cashflows": [
                {"date": "2026-07-15", "amortization": 0.0, "interest": 3.0},
                {"date": "2027-01-15", "amortization": 100.0, "interest": 3.0},
            ],
            "expected_principal": 100.0,
            "sources": [{"url": "https://example.org/contract", "retrieved_at": "2026-09-08"}],
        }],
    }


@pytest.fixture
def catalog(tmp_db, monkeypatch):
    from config.settings import settings
    from core.infrastructure.db.catalog_repository import init_db
    from apps.web.instruments_abm import save_instrument

    db = tmp_db / "test.db"
    monkeypatch.setattr(settings, "catalog_db", db)
    monkeypatch.setattr(settings, "backup_dir", tmp_db / "backups")
    init_db()
    save_instrument("Obligaciones_Negociables", {
        "ticker_ars": "OLD1O", "ticker_mep": "OLD1D", "isin": "AR1234567890",
        "short_name": "Anterior", "tipo": "HARD DOLLAR",
        "fecha_emision": "2026-01-15", "fecha_vencimiento": "2027-01-15",
    }, cashflows=[{"date": "2027-01-15", "amortization": 100, "interest": 5}])
    monkeypatch.setattr("scripts.op_guards.server_running", lambda *_: False)
    return db


def test_dry_run_does_not_write_or_backup(catalog, manifest, monkeypatch):
    before = loader.inventory(catalog)
    monkeypatch.setattr("scripts.op_guards.guard_write_snapshot", lambda *_a, **_k: pytest.fail("backup in dry-run"))
    assert loader.run_import(manifest, catalog)["planned"] == ["NEW1O"]
    assert loader.inventory(catalog) == before


def test_append_preserves_previous_and_repeat_is_noop(catalog, manifest, monkeypatch):
    before = loader.inventory(catalog)
    result = loader.run_import(manifest, catalog, apply=True)
    assert result["created"] == ["NEW1O"]
    loader.assert_preserved(before, loader.inventory(catalog))
    assert loader.inventory(result["backup"]) == before
    after = loader.inventory(catalog)
    monkeypatch.setattr("scripts.op_guards.guard_write_snapshot", lambda *_a, **_k: pytest.fail("backup for no-op"))
    assert loader.run_import(manifest, catalog, apply=True)["skipped"] == ["NEW1O"]
    assert loader.inventory(catalog) == after


@pytest.mark.parametrize("field,value", [("ticker_ars", "OLD1O"), ("ticker_mep", "OLD1D"), ("isin", "AR1234567890")])
def test_collisions_never_upsert(catalog, manifest, field, value):
    manifest["records"][0]["fields"][field] = value
    before = loader.inventory(catalog)
    with pytest.raises(ValueError, match="coincidencia"):
        loader.run_import(manifest, catalog, apply=True)
    assert loader.inventory(catalog) == before


def test_modified_existing_import_is_conflict(catalog, manifest):
    loader.run_import(manifest, catalog, apply=True)
    manifest["records"][0]["cashflows"][0]["interest"] = 7
    with pytest.raises(ValueError, match="coincidencia"):
        loader.run_import(manifest, catalog, apply=True)


@pytest.mark.parametrize("rc", [2, 3])
def test_failed_guard_writes_nothing(catalog, manifest, monkeypatch, rc):
    before = loader.inventory(catalog)
    monkeypatch.setattr("scripts.op_guards.guard_write_snapshot", lambda *_a, **_k: (rc, None))
    with pytest.raises(RuntimeError, match="preflight"):
        loader.run_import(manifest, catalog, apply=True)
    assert loader.inventory(catalog) == before


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
def test_nonfinite_or_negative_cashflow_rejected(manifest, value):
    manifest["records"][0]["cashflows"][0]["interest"] = value
    with pytest.raises(ValueError):
        loader.validate(manifest)


def test_duplicate_identity_within_batch_rejected(manifest):
    manifest["records"].append(copy.deepcopy(manifest["records"][0]))
    with pytest.raises(ValueError, match="duplicad"):
        loader.validate(manifest)


def test_cashflow_metadata_rejected_before_any_write(catalog, manifest):
    before = loader.inventory(catalog)
    manifest["records"][0]["cashflows"][0]["provenance"] = "página 2"
    with pytest.raises(ValueError, match="campos"):
        loader.run_import(manifest, catalog, apply=True)
    assert loader.inventory(catalog) == before


def test_swapped_currency_slots_are_not_an_exact_import(catalog, manifest):
    fields = manifest["records"][0]["fields"]
    fields.update(ticker_mep="NEW1D", ticker_ccl="NEW1C")
    loader.run_import(manifest, catalog, apply=True)
    state = loader.inventory(catalog)
    row = next(r for r in state["instruments"] if r["ticker"] == "NEW1O")
    row["ticker_mep"], row["ticker_ccl"] = row["ticker_ccl"], row["ticker_mep"]
    with pytest.raises(ValueError, match="coincidencia"):
        loader.plan(manifest, state)


def test_wrong_sector_and_principal_rejected(manifest):
    manifest["records"][0]["fields"]["sector_override"] = "Bancos"
    with pytest.raises(ValueError, match="sector"):
        loader.validate(manifest)
    manifest["records"][0]["fields"]["sector_override"] = "Servicios Financieros"
    manifest["records"][0]["cashflows"][-1]["amortization"] = 99
    with pytest.raises(ValueError, match="capital"):
        loader.validate(manifest)


def test_explicit_final_payment_preserves_legal_maturity(catalog, manifest):
    record = manifest["records"][0]
    record["fields"]["fecha_ultimo_pago_contractual"] = "2027-01-14"
    record["cashflows"][-1]["date"] = "2027-01-14"
    loader.run_import(manifest, catalog, apply=True)
    state = loader.inventory(catalog)
    row = next(r for r in state["instruments"] if r["ticker"] == "NEW1O")
    assert row["maturity_date"] == "2027-01-15"
    assert next(c for c in state["cashflows"] if c["ticker"] == "NEW1O" and c["amortizacion"])["fecha_pago"] == "2027-01-14"


@pytest.mark.parametrize("declared", [None, "2027-01-13", "2027-01-16"])
def test_early_final_payment_requires_matching_explicit_date(manifest, declared):
    record = manifest["records"][0]
    record["cashflows"][-1]["date"] = "2027-01-14"
    if declared:
        record["fields"]["fecha_ultimo_pago_contractual"] = declared
    with pytest.raises(ValueError, match="último flujo"):
        loader.validate(manifest)


@pytest.mark.parametrize("identity", ["ticker", "isin"])
def test_interleaved_writer_is_preserved(catalog, manifest, monkeypatch, identity):
    from apps.web import instruments_abm as abm
    original_save = abm.save_instrument
    fields = manifest["records"][0]["fields"]
    fields["ticker_mep"] = "NEW1D"
    fields["isin"] = "AR9876543210"
    competing = {**fields, "ticker_ars": "RACE1O", "ticker_mep": None,
                 "isin": None, "short_name": "Otro operador"}
    competing["ticker_mep" if identity == "ticker" else "isin"] = (
        "NEW1D" if identity == "ticker" else "AR9876543210")
    saved = {}

    def interleave(sheet, incoming, **kwargs):
        original_save(sheet, competing, cashflows=[
            {"date": "2027-01-15", "amortization": 100.0, "interest": 11.0}])
        saved["state"] = loader.inventory(catalog)
        return original_save(sheet, incoming, **kwargs)

    monkeypatch.setattr(abm, "save_instrument", interleave)
    with pytest.raises(ValueError, match="coincidencia"):
        loader.run_import(manifest, catalog, apply=True)
    assert loader.inventory(catalog) == saved["state"]
