"""Guardianes de datos del lote: capturas/procedencia en el manifiesto.

Los importes son USD por 100 VN originales, no porcentajes reescalados al VR.
Fecha contractual del corte fija, independiente del reloj de ejecución.
"""
import json
from pathlib import Path

import pytest

_MANIFEST = Path(__file__).resolve().parents[1] / "data/imports/on-2026-09-08.json"
_RECORDS = {r["requested_ticker"]: r for r in json.loads(_MANIFEST.read_text(encoding="utf-8"))["records"]}


@pytest.mark.parametrize("ticker,first_date,first_interest", [
    ("PLC7O", "2027-03-30", 5.6625),  # 9 meses × 7,55% /12.
    ("ZPC5O", "2027-03-26", 4.861643835616438),  # 273 días × 6,5% /365.
    ("TBCAO", "2027-01-03", 1.6378082191780823),  # 122 días × 4,9% /365.
    ("MR46O", "2026-12-31", 4.291666666666667),  # Stub 206 días 30/360.
])
def test_first_long_coupon_is_not_regular_coupon(ticker, first_date, first_interest):
    first = _RECORDS[ticker]["cashflows"][0]
    assert first["date"] == first_date
    assert first["interest"] == pytest.approx(first_interest, abs=1e-10)


@pytest.mark.parametrize("ticker,residual,final_principal,kind", [
    ("RZ8BO", 60.0, 22.5, "DOLLAR LINKED"),  # 30% del saldo75 previo al canje.
    ("RZBAO", 80.0, 30.0, "DOLLAR LINKED"),
    ("RZBBO", 80.0, 30.0, "HARD DOLLAR"),
    ("RZBCO", 80.0, 30.0, "HARD DOLLAR"),
])
def test_restructured_principal_keeps_original_nominal_scale(ticker, residual, final_principal, kind):
    record = _RECORDS[ticker]
    assert record["fields"]["tipo"] == kind
    future = [c for c in record["cashflows"] if c["date"] > "2026-09-09"]
    assert sum(c["amortization"] for c in future) == pytest.approx(residual, abs=1e-9)
    assert future[-1]["amortization"] == final_principal


def test_reopening_keeps_original_accrual_and_amortizes():
    record = _RECORDS["DN1AO"]
    assert record["fields"]["fecha_emision"] == "2026-04-28"
    assert record["cashflows"][0] == {"date": "2026-10-28", "interest": 4.75, "amortization": 0.0}
    principal = [(c["date"], c["amortization"]) for c in record["cashflows"] if c["amortization"]]
    assert principal == [("2031-04-28", 33.33), ("2032-04-28", 33.33), ("2033-04-28", 33.34)]


def test_issuer_contract_wins_over_byma_maturity_errors():
    assert _RECORDS["PFC4O"]["fields"]["fecha_vencimiento"] == "2030-02-28"
    assert _RECORDS["YFCPO"]["fields"]["fecha_vencimiento"] == "2029-08-18"
    assert _RECORDS["YFCPO"]["fields"]["base calculo"] == "30/360"


def test_sector_and_law_are_independent_of_payment_venue():
    assert _RECORDS["VSCYO"]["fields"]["ley_aplicable"] == "Argentina"
    assert _RECORDS["VSCYO"]["fields"]["moneda_pago_contractual"] == "USD_CABLE"
    assert _RECORDS["DEC4O"]["fields"]["sector_override"] == "Utilities (Luz / Gas)"
    assert _RECORDS["ZPC5O"]["fields"]["sector_override"] == "Real Estate"
    assert _RECORDS["PFC4O"]["fields"]["sector_override"] == "Agro / Alimentos"


def test_conditional_pik_is_not_silently_loaded_as_fixed_cash():
    assert not {"MR43O", "MR44O", "MR45O", "MR47O", "MR50O"}.intersection(_RECORDS)


def test_hattrick_final_payment_does_not_replace_legal_maturity():
    record = _RECORDS["HT2MD"]
    assert record["fields"]["fecha_vencimiento"] == "2029-06-30"
    assert record["fields"]["fecha_ultimo_pago_contractual"] == "2029-06-29"
    assert record["cashflows"][-1]["date"] == "2029-06-29"
    assert record["cashflows"][-1]["amortization"] == 9.1
    # 60 días, excluye el sábado de vencimiento. Fuente: prospecto pp17-18.
    assert record["cashflows"][-1]["interest"] == pytest.approx(0.1308904109589041, abs=1e-10)
    assert sum(c["amortization"] > 0 for c in record["cashflows"]) == 11


def test_dqs_is_hard_dollar_with_single_annual_coupon():
    record = _RECORDS["DQS1L"]
    assert record["fields"]["tipo"] == "HARD DOLLAR"
    assert record["fields"]["fecha_emision"] == "2026-08-27"
    assert record["cashflows"] == [{"date": "2027-08-27", "amortization": 100.0, "interest": 7.0}]
