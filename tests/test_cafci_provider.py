"""Tests del `CAFCIProvider` — focus en el join catalogo↔matriz, el parseo
de números-string, y el filtrado/orden/búsqueda de `list_funds`. Sin red ni
disco: se inyecta un dataset class-level y se cierra el gate de fetch."""

from datetime import date

import pytest

from core.infrastructure.cafci_provider import (
    CAFCIProvider, _parse_payload, _to_float, _norm,
)


SAMPLE = {
    "catalogo": {
        "fondos": [
            {
                "id": 304, "nombre": "1810 Ahorro", "tipo_dinero": "Clásico",
                "dias_liquidacion": 0, "valuacion": "D",
                "sociedad_gerente": {"id": 241, "nombre": "Proahorro"},
                "moneda": {"id": 1, "nombre": "Peso Argentina"},
                "tipo_renta": {"id": 4, "nombre": "Mercado de Dinero"},
                "clases": [
                    {"id": 308, "nombre": "1810 Ahorro",
                     "moneda": {"id": 1, "nombre": "Peso Argentina"}},
                    {"id": 309, "nombre": "1810 Ahorro USD",
                     "moneda": {"id": 2, "nombre": "Dolar Estadounidense"}},
                ],
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
        },
    },
}


@pytest.fixture
def provider():
    """Provider con dataset inyectado y gate de fetch cerrado (no toca red)."""
    parsed = _parse_payload(SAMPLE)
    CAFCIProvider._dataset = parsed
    CAFCIProvider._disk_loaded = True
    CAFCIProvider._last_attempt = date.today()
    CAFCIProvider._last_fail_ts = 0.0
    yield CAFCIProvider()
    CAFCIProvider._dataset = {"meta": {}, "funds": []}
    CAFCIProvider._disk_loaded = False
    CAFCIProvider._last_attempt = None


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


class TestListFunds:
    def test_filter_by_tipo(self, provider):
        out = provider.list_funds(tipo_renta="Mercado de Dinero")
        assert sorted(f["clase_id"] for f in out) == [308, 309]

    def test_filter_by_moneda(self, provider):
        out = provider.list_funds(moneda="Peso Argentina")
        assert sorted(f["clase_id"] for f in out) == [308, 500]

    def test_sort_desc_by_period_metric(self, provider):
        out = provider.list_funds(sort="mes_1", metric="tna", direction="desc")
        assert [f["clase_id"] for f in out] == [500, 308, 309]  # 45 > 21.45 > 4.0

    def test_sort_asc(self, provider):
        out = provider.list_funds(sort="mes_1", metric="tna", direction="asc")
        assert [f["clase_id"] for f in out] == [309, 308, 500]

    def test_missing_values_sink_last(self, provider):
        # dias_90 tna: 500=50, 308=24.82, 309=None → None último aunque sea desc.
        out = provider.list_funds(sort="dias_90", metric="tna", direction="desc")
        assert [f["clase_id"] for f in out] == [500, 308, 309]
        # Y también último en asc.
        out_asc = provider.list_funds(sort="dias_90", metric="tna", direction="asc")
        assert out_asc[-1]["clase_id"] == 309

    def test_search_accent_insensitive(self, provider):
        assert sorted(f["clase_id"] for f in provider.list_funds(query="ahorro")) == [308, 309]
        assert [f["clase_id"] for f in provider.list_funds(query="delta")] == [500]  # por sociedad

    def test_limit(self, provider):
        out = provider.list_funds(sort="mes_1", metric="tna", limit=2)
        assert [f["clase_id"] for f in out] == [500, 308]

    def test_invalid_sort_falls_back(self, provider):
        # sort desconocido → mes_1; metric desconocido → tna. No explota.
        out = provider.list_funds(sort="zzz", metric="zzz")
        assert [f["clase_id"] for f in out] == [500, 308, 309]


class TestMetaAndDetail:
    def test_meta_options_with_counts(self, provider):
        meta = provider.get_meta()
        tipos = {o["value"]: o["count"] for o in meta["tipo_renta_options"]}
        assert tipos == {"Mercado de Dinero": 2, "Renta Fija": 1}
        monedas = {o["value"]: o["count"] for o in meta["moneda_options"]}
        assert monedas == {"Peso Argentina": 2, "Dolar Estadounidense": 1}

    def test_get_fund_roundtrip(self, provider):
        rec = provider.get_fund(308)
        assert rec is not None and rec["fondo_nombre"] == "1810 Ahorro"
        # acepta string
        assert provider.get_fund("500")["clase_id"] == 500

    def test_get_fund_missing(self, provider):
        assert provider.get_fund(99999) is None
        assert provider.get_fund("not-an-int") is None
