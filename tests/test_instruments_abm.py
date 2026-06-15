"""Tests del módulo `apps.web.instruments_abm` (backend SQLite, §5.5).

Foco: CRUD transaccional sobre SQLite. La fixture `abm_db` apunta el engine a
una .db temporal sembrada desde el Excel real, sin tocar la catalog.db de prod.
"""

import pytest

from config.settings import settings
from core.infrastructure.db import engine as db_engine
from core.infrastructure.db.catalog_repository import ingest_from_excel

from apps.web.instruments_abm import (
    _parse_cashflows,
    delete_instrument,
    get_instrument,
    list_instruments,
    list_instruments_coverage,
    save_cashflows,
    save_instrument,
)


@pytest.fixture
def abm_db(tmp_path):
    """Engine apuntado a una .db temporal sembrada desde el master Excel."""
    db_engine.configure(tmp_path / "abm_test.db")
    try:
        ingest_from_excel(str(settings.master_xlsx))
        yield
    finally:
        db_engine.configure(settings.catalog_db)  # restaurar la .db real


# --------------------------------------------------------------------------- #
# _parse_cashflows — validación de input del frontend (función pura)
# --------------------------------------------------------------------------- #

class TestParseCashflows:
    def test_valid_input(self):
        rows = [
            {"date": "2026-07-09", "amortization": 8.0, "interest": 0.27},
            {"date": "2027-01-09", "amortization": 8.0, "interest": 0.24},
        ]
        parsed = _parse_cashflows(rows)
        assert len(parsed) == 2
        assert parsed[0][0].isoformat() == "2026-07-09"
        assert parsed[0][1] == 8.0
        assert parsed[0][2] == 0.27

    def test_sorts_by_date(self):
        rows = [
            {"date": "2027-01-09", "amortization": 1.0, "interest": 0.1},
            {"date": "2026-07-09", "amortization": 2.0, "interest": 0.2},
        ]
        parsed = _parse_cashflows(rows)
        assert parsed[0][0].isoformat() == "2026-07-09"
        assert parsed[1][0].isoformat() == "2027-01-09"

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError, match="invalid date"):
            _parse_cashflows([{"date": "not-a-date", "amortization": 1, "interest": 0}])

    def test_empty_date_skipped(self):
        rows = [
            {"date": "", "amortization": 1, "interest": 0},
            {"date": "2026-07-09", "amortization": 2, "interest": 0},
        ]
        parsed = _parse_cashflows(rows)
        assert len(parsed) == 1

    def test_missing_amort_interest_default_zero(self):
        parsed = _parse_cashflows([{"date": "2026-07-09"}])
        assert parsed[0][1] == 0.0
        assert parsed[0][2] == 0.0


# --------------------------------------------------------------------------- #
# save_instrument transaccional (row + cashflows en una sola txn SQLAlchemy)
# --------------------------------------------------------------------------- #

class TestSaveInstrumentTransactional:
    def test_save_row_only_no_cashflows(self, abm_db):
        """Sin cashflows kwarg → solo escribe la fila (synth on-the-fly)."""
        result = save_instrument(
            "Soberanos",
            {
                "ticker": "TESTROW",
                "short_name": "TESTROW",
                "tipo": "BONAR",
                "fecha_emision": "2025-01-01",
                "fecha_vencimiento": "2030-01-01",
                "cupon anual %": "1.5",
                "frecuencia pagos": "2",
            },
        )
        assert result["action"] == "created"
        assert result["ticker"] == "TESTROW"
        assert "cashflows" not in result

    def test_save_with_cashflows_atomic(self, abm_db):
        """Con cashflows, row + cashflows persisten juntos."""
        cashflows = [
            {"date": "2026-01-01", "amortization": 50.0, "interest": 1.0},
            {"date": "2027-01-01", "amortization": 50.0, "interest": 0.5},
        ]
        result = save_instrument(
            "Soberanos",
            {
                "ticker": "TXN1",
                "short_name": "TXN1",
                "tipo": "BONAR",
                "fecha_emision": "2025-01-01",
                "fecha_vencimiento": "2027-01-01",
                "cupon anual %": "2.0",
                "frecuencia pagos": "2",
            },
            cashflows=cashflows,
        )
        assert result["cashflows"] == 2

        loaded = get_instrument("TXN1")
        assert loaded is not None
        assert loaded["fields"]["tipo"] == "BONAR"
        assert len(loaded["cashflows"]) == 2
        assert loaded["cashflows_source"] == "sheet"

    def test_save_invalid_cashflow_aborts_everything(self, abm_db):
        """Cashflow con fecha inválida → ni la row ni los cashflows persisten."""
        with pytest.raises(ValueError, match="invalid date"):
            save_instrument(
                "Soberanos",
                {
                    "ticker": "BADCF",
                    "short_name": "BADCF",
                    "tipo": "BONAR",
                    "fecha_emision": "2025-01-01",
                    "fecha_vencimiento": "2030-01-01",
                    "cupon anual %": "1.0",
                },
                cashflows=[{"date": "GARBAGE", "amortization": 100, "interest": 0}],
            )
        assert get_instrument("BADCF") is None

    def test_save_soberano_multi_ticker_creates_legs(self, abm_db):
        """Form nuevo con 3 tickers → 3 especies, mismos cashflows, 1 grupo."""
        from apps.web.instruments_abm import list_instruments
        save_instrument("Soberanos", {
            "ticker_ars": "ZZ9", "ticker_mep": "ZZ9D", "ticker_ccl": "ZZ9C",
            "short_name": "ZZ9", "tipo": "BONAR",
            "fecha_emision": "2025-01-01", "fecha_vencimiento": "2030-01-01",
            "cupon anual %": "1.0", "frecuencia pagos": "2",
        })
        for t in ("ZZ9", "ZZ9D", "ZZ9C"):
            assert get_instrument(t) is not None
        # cashflows idénticos entre especies
        cf_ars = get_instrument("ZZ9")["cashflows"]
        cf_mep = get_instrument("ZZ9D")["cashflows"]
        assert cf_ars and cf_ars == cf_mep
        # 1 sola entrada consolidada
        by_key = {it["key"]: it for it in list_instruments()}
        assert set(by_key["ZZ9"]["tickers"]) == {"ZZ9", "ZZ9D", "ZZ9C"}

    def test_save_soberano_sync_removes_cleared_leg(self, abm_db):
        """Reeditar el bono dejando vacío un slot borra esa especie."""
        save_instrument("Soberanos", {
            "ticker_ars": "ZZ8", "ticker_mep": "ZZ8D", "ticker_ccl": "ZZ8C",
            "short_name": "ZZ8", "tipo": "GLOBAL",
            "fecha_emision": "2025-01-01", "fecha_vencimiento": "2030-01-01",
            "cupon anual %": "1.0", "frecuencia pagos": "2",
        })
        # re-guardar sin CABLE → ZZ8C debe desaparecer; ARS/MEP permanecen
        save_instrument("Soberanos", {
            "ticker_ars": "ZZ8", "ticker_mep": "ZZ8D", "ticker_ccl": "",
            "short_name": "ZZ8", "tipo": "GLOBAL",
            "fecha_emision": "2025-01-01", "fecha_vencimiento": "2030-01-01",
            "cupon anual %": "1.0", "frecuencia pagos": "2",
        })
        assert get_instrument("ZZ8") is not None
        assert get_instrument("ZZ8D") is not None
        assert get_instrument("ZZ8C") is None

    def test_delete_soberano_group_removes_all_legs(self, abm_db):
        save_instrument("Soberanos", {
            "ticker_ars": "ZZ7", "ticker_mep": "ZZ7D", "ticker_ccl": "",
            "short_name": "ZZ7", "tipo": "BONAR",
            "fecha_emision": "2025-01-01", "fecha_vencimiento": "2030-01-01",
            "cupon anual %": "1.0", "frecuencia pagos": "2",
        })
        result = delete_instrument("ZZ7")  # key = grupo
        assert result["action"] == "deleted"
        assert set(result["tickers"]) == {"ZZ7", "ZZ7D"}
        assert get_instrument("ZZ7") is None and get_instrument("ZZ7D") is None

    def test_save_updates_existing(self, abm_db):
        """Editar un ticker existente devuelve 'updated' y persiste el cambio."""
        save_instrument("Soberanos", {
            "ticker": "UPDT1", "short_name": "UPDT1", "tipo": "BONAR",
            "fecha_emision": "2025-01-01", "fecha_vencimiento": "2030-01-01",
            "cupon anual %": "1.0", "frecuencia pagos": "2",
        })
        result = save_instrument("Soberanos", {
            "ticker": "UPDT1", "short_name": "UPDT1", "tipo": "BONAR",
            "fecha_emision": "2025-01-01", "fecha_vencimiento": "2030-01-01",
            "cupon anual %": "2.5", "frecuencia pagos": "2",
        })
        assert result["action"] == "updated"
        loaded = get_instrument("UPDT1")
        assert str(loaded["fields"]["cupon anual %"]) in ("2.5", "2,5")


# --------------------------------------------------------------------------- #
# save_cashflows standalone
# --------------------------------------------------------------------------- #

class TestSaveCashflows:
    def test_replace_existing_cashflows(self, abm_db):
        """Reemplaza TODAS las filas del ticker (delete + insert)."""
        result = save_cashflows("AL30D", [
            {"date": "2030-01-01", "amortization": 100, "interest": 5},
        ])
        assert result["count"] == 1
        loaded = get_instrument("AL30D")
        assert loaded["cashflows_source"] == "sheet"
        assert len(loaded["cashflows"]) == 1
        assert loaded["cashflows"][0]["amortization"] == 100

    def test_empty_cashflows_clears(self, abm_db):
        """Pasar [] borra todas las cashflows → fallback a synth/empty."""
        save_cashflows("AL30D", [])
        loaded = get_instrument("AL30D")
        assert loaded["cashflows_source"] in ("synth", "empty")


# --------------------------------------------------------------------------- #
# delete_instrument
# --------------------------------------------------------------------------- #

class TestDeleteInstrument:
    def test_delete_removes_row_and_cashflows(self, abm_db):
        save_instrument("Soberanos", {
            "ticker": "DELME", "short_name": "DELME", "tipo": "BONAR",
            "fecha_emision": "2025-01-01", "fecha_vencimiento": "2030-01-01",
            "cupon anual %": "1.0",
        }, cashflows=[{"date": "2030-01-01", "amortization": 100, "interest": 0}])
        assert get_instrument("DELME") is not None

        result = delete_instrument("DELME")
        assert result["action"] == "deleted"
        assert result["ticker"] == "DELME"
        assert get_instrument("DELME") is None

    def test_delete_nonexistent_returns_not_found(self, abm_db):
        result = delete_instrument("NEVEREXISTED")
        assert result["action"] == "not_found"


# --------------------------------------------------------------------------- #
# list_instruments
# --------------------------------------------------------------------------- #

class TestListInstruments:
    def test_returns_known_tickers(self, abm_db):
        # Soberanos consolidados: las especies aparecen en `tickers` de su grupo.
        all_tickers = {t for it in list_instruments() for t in it["tickers"]}
        assert "AL30D" in all_tickers
        assert "S29Y6" in all_tickers

    def test_returns_sheet_per_ticker(self, abm_db):
        sheet_by_ticker = {}
        for it in list_instruments():
            for t in it["tickers"]:
                sheet_by_ticker[t] = it["sheet"]
        assert sheet_by_ticker.get("AL30D") == "Soberanos"
        assert sheet_by_ticker.get("S29Y6") == "Tasa_Fija"

    def test_soberano_legs_consolidated_into_one_group(self, abm_db):
        """AL30 / AL30D / AL30C → 1 sola entrada (grupo 'AL30')."""
        by_key = {it["key"]: it for it in list_instruments()}
        assert "AL30" in by_key
        grp = by_key["AL30"]
        assert grp["sheet"] == "Soberanos"
        assert set(grp["tickers"]) >= {"AL30", "AL30D", "AL30C"}
        # las especies no aparecen como entradas top-level sueltas
        assert "AL30D" not in by_key and "AL30C" not in by_key


# --------------------------------------------------------------------------- #
# _safe_synth — contrato del except acotado
# --------------------------------------------------------------------------- #

class TestSafeSynth:
    """Documenta y pinnea el contrato del `except` acotado de `_safe_synth`.

    - Input inválido (ValueError/KeyError/TypeError) → [] (no tumba el form).
    - AttributeError (bug interno en synth) → PROPAGA (no se traga silenciosamente).
    """

    def test_bad_input_returns_empty(self):
        from apps.web.instruments_abm import _safe_synth
        # Campo frecuencia no parseable → ValueError dentro de synth → []
        result = _safe_synth({"tipo": "BONAR", "frecuencia pagos": "no-es-numero",
                               "fecha_emision": "2025-01-01",
                               "fecha_vencimiento": "2030-01-01"})
        assert result == []

    def test_missing_required_fields_returns_empty(self):
        from apps.web.instruments_abm import _safe_synth
        # Sin campos mínimos → KeyError/ValueError → []
        assert _safe_synth({}) == []

    def test_attribute_error_propagates(self):
        """Un AttributeError dentro de synth NO se silencia: signalaría un bug interno."""
        import unittest.mock as mock
        from apps.web.instruments_abm import _safe_synth
        # _safe_synth importa synth_cashflows on-the-fly; parchear donde vive.
        with mock.patch("core.domain.cashflow_synth.synth_cashflows",
                        side_effect=AttributeError("bug interno simulado")):
            with pytest.raises(AttributeError, match="bug interno simulado"):
                _safe_synth({"tipo": "BONAR", "fecha_emision": "2025-01-01",
                              "fecha_vencimiento": "2030-01-01",
                              "frecuencia pagos": "2", "cupon anual %": "5.0"})


# --------------------------------------------------------------------------- #
# C2/C3 — performance de lookups (índices ticker_mep/ccl + noload de cashflows)
# --------------------------------------------------------------------------- #

def _capture_sql(fn):
    """Corre `fn()` capturando todo el SQL emitido sobre el engine vivo."""
    from sqlalchemy import event
    from core.infrastructure.db import engine as db_engine
    eng = db_engine.get_engine()
    sql: list = []

    def _cap(conn, cursor, statement, params, context, executemany):
        sql.append(statement)

    event.listen(eng, "before_cursor_execute", _cap)
    try:
        result = fn()
    finally:
        event.remove(eng, "before_cursor_execute", _cap)
    return result, sql


class TestLookupPerformance:
    def test_find_bond_row_by_pata_is_index_backed(self, abm_db):
        """C2: resolver un bono por su pata MEP/CABLE usa un WHERE sobre la columna
        indexada (ix_instr_mep/ccl), no un select-all + filtro Python. Comportamiento
        intacto (la pata resuelve a la fila primaria); el SQL ya NO es un scan."""
        from core.infrastructure.db.engine import SessionLocal
        from apps.web.instruments_abm import _find_bond_row, _find_bond_rows
        save_instrument("Soberanos", {
            "ticker_ars": "ZZ6", "ticker_mep": "ZZ6D", "ticker_ccl": "ZZ6C",
            "short_name": "ZZ6", "tipo": "BONAR",
            "fecha_emision": "2025-01-01", "fecha_vencimiento": "2030-01-01",
            "cupon anual %": "1.0", "frecuencia pagos": "2",
        })

        def _run():
            with SessionLocal() as s:
                row = _find_bond_row(s, "ZZ6D")           # PK miss → lookup por pata
                rows = _find_bond_rows(s, ["ZZ6D"])
                return row, rows

        (row, rows), sql = _capture_sql(_run)
        # comportamiento: la pata MEP resuelve a la fila primaria ZZ6
        assert row is not None and row.ticker == "ZZ6"
        assert any(o.ticker == "ZZ6" for o in rows)
        # el SQL del lookup por pata trae un WHERE sobre ticker_mep (en el viejo código
        # se filtraba en Python → ningún SELECT mencionaba ticker_mep en su WHERE)
        pata_q = [q for q in sql if "FROM instruments" in q and "ticker_mep" in q]
        assert pata_q, sql

    def test_coverage_does_not_select_in_cashflows(self, abm_db):
        """C3: list_instruments_coverage NO dispara el SELECT-IN de cashflows del
        relationship selectin (usa noload); el % viene del conteo agregado aparte."""
        rows, sql = _capture_sql(list_instruments_coverage)
        assert rows
        # 0 SELECT-IN de cashflows (el selectin pelado emitiría 'FROM cashflows' sin count)
        cf_in = [s for s in sql if "FROM cashflows" in s and "count(" not in s.lower()]
        assert cf_in == [], cf_in
        # SÍ corre la query de conteo agregado de cashflows (de donde sale el %)
        cf_cnt = [s for s in sql if "count(" in s.lower() and "cashflows" in s.lower()]
        assert len(cf_cnt) == 1
        # anti-regresión del %: el dato de cashflows sigue presente
        assert all("pct" in r and "cf" in r for r in rows)
        assert any(r["cf"] > 0 for r in rows)


class TestSectorCategory:
    """La columna Categoría (sector) del ABM muestra el sector EFECTIVO de cada ON con
    la MISMA etiqueta corta que el monitor de ON (Energía / Serv. Financieros / …):
    override manual si está, si no derivado del emisor (no '—')."""

    _BASE = {
        "tipo": "HARD DOLLAR", "ley_aplicable": "Argentina",
        "fecha_emision": "2025-01-15", "fecha_vencimiento": "2027-01-15",
        "cupon anual %": "8", "frecuencia pagos": "2",
        "base calculo": "ACT/365", "tipo amortizacion": "bullet",
    }

    def test_derived_sector_shows_monitor_short_label(self, abm_db):
        save_instrument("Obligaciones_Negociables",
                        {**self._BASE, "ticker_ars": "ZZSEC0", "short_name": "YPF SA"})
        r = {x["key"]: x for x in
             list_instruments_coverage(sheet="Obligaciones_Negociables")}["ZZSEC0"]
        assert r["sector_auto"] is True
        # derivado del emisor (key "Energía / Petróleo & Gas") → etiqueta del monitor
        assert r["vals"]["sector_override"] == "Energía"

    def test_manual_override_shown_with_short_label(self, abm_db):
        save_instrument("Obligaciones_Negociables",
                        {**self._BASE, "ticker_ars": "ZZSEC1", "short_name": "Frobnicate SA",
                         "sector_override": "Servicios Financieros"})
        r = {x["key"]: x for x in
             list_instruments_coverage(sheet="Obligaciones_Negociables")}["ZZSEC1"]
        assert r["sector_auto"] is False
        # value guardado = key canónica; se muestra con la etiqueta corta del monitor
        assert r["vals"]["sector_override"] == "Serv. Financieros"

    def test_form_select_uses_monitor_labels_with_key_values(self, abm_db):
        """El <select> de Categoría ofrece las etiquetas del monitor como texto, pero el
        value es la key canónica (lo que matchea sector_for)."""
        from fastapi.testclient import TestClient
        from apps.web.app import app
        with TestClient(app) as c:
            html = c.get("/abm/form?sheet=Obligaciones_Negociables").text
        assert 'value="Servicios Financieros"' in html      # value = key canónica
        assert '>Serv. Financieros<' in html                # texto = etiqueta del monitor
        # el & de la key se escapa en el atributo (autoescape); el texto es la etiqueta corta
        assert '>Energía<' in html and 'value="Energía / Petróleo &amp; Gas"' in html

    def test_coverage_price_prefers_mep_leg(self, abm_db):
        """El precio de la fila ABM toma la pata MEP (…D, en USD), no la pata pesos (…O)."""
        save_instrument("Obligaciones_Negociables",
                        {**self._BASE, "ticker_ars": "ZZP0O", "ticker_mep": "ZZP0D",
                         "short_name": "PRICE TEST"})
        prices = {"ZZP0O": 150990.0, "ZZP0D": 98.5}
        r = {x["key"]: x for x in list_instruments_coverage(
            price_of=prices.get, sheet="Obligaciones_Negociables")}["ZZP0O"]
        assert r["price"] == 98.5   # la pata MEP (…D), no el peso 150990
