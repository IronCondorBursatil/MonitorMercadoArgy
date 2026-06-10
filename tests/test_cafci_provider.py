"""Tests del `_parse_payload` del CAFCIProvider — join catalogo↔matriz, parseo de
números-string y conservación de los campos ricos. Sin red ni disco."""

import pytest

from core.domain.fci.derive import norm as _norm, to_float as _to_float
from core.infrastructure.cafci_provider import _parse_payload


SAMPLE = {
    "catalogo": {
        "fondos": [
            {
                "id": 304, "nombre": "1810 Ahorro", "tipo_dinero": "Clásico",
                "dias_liquidacion": 0, "valuacion": "D", "estado": 1,
                "sociedad_gerente": {"id": 241, "nombre": "Proahorro"},
                "sociedad_depositaria": {"id": 116, "nombre": "Banco Credicoop"},
                "moneda": {"id": 1, "nombre": "Peso Argentina"},
                "tipo_renta": {"id": 4, "nombre": "Mercado de Dinero"},
                "region": {"id": 1, "nombre": "Argentina"},
                "horizonte": {"id": 1, "nombre": "Corto Plazo"},
                "duration": {"id": 3, "nombre": "Menor o Igual a 1 Año"},
                "mm_puro": False, "mm_indice": False,
                "objetivo": "Cartera de bajo riesgo.", "inicio": "2000-09-14",
                "clases": [
                    {"id": 308, "nombre": "1810 Ahorro",
                     "moneda": {"id": 1, "nombre": "Peso Argentina"},
                     "inversion_minima": 5000, "ticker_isin": "ARX", "ticker_bloomberg": "BBG1",
                     "honorarios": {"ingreso": "0.0", "rescate": "0.5",
                                    "administracion_gerente": "0.1",
                                    "administracion_depositaria": "0.15",
                                    "gasto_ordinario_gestion": "0.0"}},
                    {"id": 309, "nombre": "1810 Ahorro USD",
                     "moneda": {"id": 2, "nombre": "Dolar Estadounidense"}},
                ],
            },
            {   # estado=0 (cerrado) → todo el fondo se descarta aunque su clase tenga precio
                "id": 77, "nombre": "Fondo Cerrado", "estado": 0,
                "sociedad_gerente": {"id": 9, "nombre": "X AM"},
                "moneda": {"id": 1, "nombre": "Peso Argentina"},
                "tipo_renta": {"id": 3, "nombre": "Renta Fija"},
                "clases": [{"id": 600, "nombre": "Cerrado A",
                            "moneda": {"id": 1, "nombre": "Peso Argentina"}}],
            },
            {
                "id": 99, "nombre": "Delta Renta Fija", "tipo_dinero": None,
                "dias_liquidacion": 1, "valuacion": "D",
                "sociedad_gerente": {"id": 50, "nombre": "Delta AM"},
                "moneda": {"id": 1, "nombre": "Peso Argentina"},
                "tipo_renta": {"id": 3, "nombre": "Renta Fija"},
                "clases": [
                    {"id": 500, "nombre": "Delta RF A",
                     "moneda": {"id": 1, "nombre": "Peso Argentina"}},
                    # 501 está en el catálogo pero NO en la matriz → debe dropearse.
                    {"id": 501, "nombre": "Delta RF sin precio",
                     "moneda": {"id": 1, "nombre": "Peso Argentina"}},
                ],
            },
        ],
    },
    "matriz": {
        "fecha_base": "2026-05-20",
        "generated_at": "2026-05-20T21:38:07-03:00",
        "clases": {
            "308": {
                "valor_cuotaparte": "230993.537", "fecha_valor": "2026-05-20",
                "dias_7": {"tna": "19.83", "directo": "0.38"},
                "mes_1": {"tna": "21.45", "directo": "1.82"},
                "dias_90": {"tna": "24.82", "directo": "6.12"},
                "dias_180": {"tna": "26.79", "directo": "13.28"},
                "ytd": {"tna": "26.42", "directo": "8.76"},
                "meses_12": {"tna": "33.86", "directo": "33.86"},
            },
            "309": {
                "valor_cuotaparte": "100.5", "fecha_valor": "2026-05-20",
                "dias_7": {"tna": "3.1", "directo": "0.06"},
                "mes_1": {"tna": "4.0", "directo": "0.3"},
                "dias_90": {"tna": None, "directo": None},  # sin dato → None
                "dias_180": {}, "ytd": {}, "meses_12": {},
            },
            "500": {
                "valor_cuotaparte": "5000.0", "fecha_valor": "2026-05-20",
                "dias_7": {"tna": "40.0", "directo": "0.7"},
                "mes_1": {"tna": "45.0", "directo": "3.5"},
                "dias_90": {"tna": "50.0", "directo": "12.0"},
                "dias_180": {"tna": "48.0", "directo": "22.0"},
                "ytd": {"tna": "47.0", "directo": "18.0"},
                "meses_12": {"tna": "55.0", "directo": "55.0"},
            },
            "600": {  # clase con precio pero su fondo está cerrado (estado=0)
                "valor_cuotaparte": "1.0", "fecha_valor": "2026-05-20",
                "mes_1": {"tna": "10.0", "directo": "0.8"},
            },
        },
    },
}


class TestHelpers:
    def test_to_float(self):
        assert _to_float("230993.537") == pytest.approx(230993.537)
        assert _to_float("") is None
        assert _to_float(None) is None
        assert _to_float("no-num") is None
        assert _to_float(5) == 5.0

    def test_norm_strips_accents_and_lowercases(self):
        assert _norm("Dinámico") == "dinamico"
        assert _norm("AHORRO") == "ahorro"
        assert _norm(None) == ""


class TestParsePayload:
    def test_joins_and_drops_unpriced(self):
        parsed = _parse_payload(SAMPLE)
        ids = sorted(f["clase_id"] for f in parsed["funds"])
        # 501 no tiene entrada en matriz → dropeada. 308/309/500 quedan.
        assert ids == [308, 309, 500]
        assert parsed["meta"]["total"] == 3
        assert parsed["meta"]["fecha_base"] == "2026-05-20"

    def test_record_shape(self):
        parsed = _parse_payload(SAMPLE)
        rec = next(f for f in parsed["funds"] if f["clase_id"] == 308)
        assert rec["fondo_nombre"] == "1810 Ahorro"
        assert rec["tipo_renta"] == "Mercado de Dinero"
        assert rec["moneda"] == "Peso Argentina"
        assert rec["sociedad"] == "Proahorro"
        assert rec["dias_liquidacion"] == 0
        assert rec["vcp"] == pytest.approx(230993.537)
        assert rec["rend"]["mes_1"]["tna"] == pytest.approx(21.45)
        assert rec["rend"]["meses_12"]["directo"] == pytest.approx(33.86)

    def test_clase_moneda_overrides_fondo_moneda(self):
        parsed = _parse_payload(SAMPLE)
        usd = next(f for f in parsed["funds"] if f["clase_id"] == 309)
        assert usd["moneda"] == "Dolar Estadounidense"

    def test_missing_period_is_none(self):
        parsed = _parse_payload(SAMPLE)
        rec = next(f for f in parsed["funds"] if f["clase_id"] == 309)
        assert rec["rend"]["dias_90"]["tna"] is None
        assert rec["rend"]["ytd"]["directo"] is None

    def test_empty_payload(self):
        parsed = _parse_payload({})
        assert parsed["funds"] == []
        assert parsed["meta"]["total"] == 0

    def test_drops_inactive_fondos(self):
        # clase 600 tiene precio en la matriz pero su fondo está estado=0 → no aparece.
        parsed = _parse_payload(SAMPLE)
        assert all(f["clase_id"] != 600 for f in parsed["funds"])


class TestParsePayloadRich:
    def test_keeps_rich_fields(self):
        rec = next(f for f in _parse_payload(SAMPLE)["funds"] if f["clase_id"] == 308)
        # honorarios → fee_admin = gerente(0.1)+depositaria(0.15)+gasto(0.0) = 0.25
        assert rec["fee_admin"] == pytest.approx(0.25)
        assert rec["fee_in"] == pytest.approx(0.0)
        assert rec["fee_out"] == pytest.approx(0.5)
        assert rec["inversion_minima"] == 5000
        assert rec["ticker_isin"] == "ARX"
        assert rec["ticker_bloomberg"] == "BBG1"
        assert rec["depositaria"] == "Banco Credicoop"
        assert rec["region"] == "Argentina"
        assert rec["horizonte"] == "Corto Plazo"
        assert rec["duration"] == "Menor o Igual a 1 Año"
        assert rec["objetivo"] == "Cartera de bajo riesgo."
        assert rec["inicio"] == "2000-09-14"
        assert rec["mm_puro"] is False

    def test_missing_rich_fields_are_none_or_zero(self):
        # fondo 99 (Delta) no trae honorarios/region/etc → degradan a None / 0.
        rec = next(f for f in _parse_payload(SAMPLE)["funds"] if f["clase_id"] == 500)
        assert rec["fee_admin"] == 0
        assert rec["fee_out"] is None
        assert rec["region"] is None
        assert rec["inversion_minima"] is None
