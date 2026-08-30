"""Header strip (dólares + reservas + riesgo país): armado de cards y render del
fragmento. Sin red — usa stubs para FX y contexto fijo para el template."""

from apps.web.routers.header import _TEMPLATES, _dolar_cards, _riesgo_pais


class _FakeFx:
    def __init__(self, quotes):
        self._q = quotes

    def get_all(self):
        return self._q


def test_dolar_cards_orders_known_casas_and_keeps_unknown():
    fx = _FakeFx({
        # desordenadas a propósito + una casa no mapeada ("solidario")
        "blue": {"nombre": "Blue", "compra": 1400.0, "venta": 1420.0},
        "oficial": {"nombre": "Oficial", "compra": 1100.0, "venta": 1150.0},
        "contadoconliqui": {"nombre": "Contado con liquidación", "compra": 1450.0, "venta": 1470.0},
        "solidario": {"nombre": "Solidario", "compra": 1500.0, "venta": 1530.0},
        "rota": {"nombre": "Rota", "compra": None, "venta": None},  # se descarta
    })
    cards = _dolar_cards(fx)
    labels = [c["label"] for c in cards]
    # oficial va antes que blue (orden curado); CCL usa label corto
    assert labels.index("Oficial") < labels.index("Blue")
    assert "CCL" in labels
    # casa no mapeada se incluye con su nombre crudo, al final
    assert "Solidario" in labels
    assert labels.index("Solidario") > labels.index("Blue")
    # casa sin venta se descarta
    assert "Rota" not in labels
    # sin cierre previo → sin variación
    assert all(c["var_pct"] is None for c in cards)


def test_dolar_cards_variation_vs_prev_close():
    fx = _FakeFx({
        "oficial": {"nombre": "Oficial", "compra": 1420.0, "venta": 1430.0},
        "blue": {"nombre": "Blue", "compra": 1430.0, "venta": 1440.0},
    })
    # cierre de ayer: oficial 1425 (sube), blue 1450 (baja); 'cripto' sin live → ignorado
    prev = {"oficial": 1425.0, "blue": 1450.0, "cripto": 1400.0}
    by_label = {c["label"]: c for c in _dolar_cards(fx, prev)}
    assert by_label["Oficial"]["var_pct"] == (1430.0 - 1425.0) / 1425.0 * 100.0
    assert by_label["Blue"]["var_pct"] < 0  # 1440 < 1450


def test_dolar_cards_empty_on_provider_failure():
    class _Boom:
        def get_all(self):
            raise RuntimeError("upstream down")

    assert _dolar_cards(_Boom(), {"oficial": 1425.0}) == []


def test_fragment_renders_with_full_context():
    html = _TEMPLATES.get_template("fragments/header_cards.html").render(
        dolares=[{"label": "Oficial", "compra": 1100.0, "venta": 1150.0, "var_pct": 0.7}],
        reservas=41234.0,
        reservas_delta=-120.0,
        riesgo_pais={"valor": 750, "fecha": "2026-05-26", "delta_abs": 15, "delta_pct": 2.0},
    )
    # puntas bid/offer: compra (1,100) y venta (1,150)
    assert "Oficial" in html
    assert '<span class="bid">1,100</span>' in html
    assert '<span class="ask">1,150</span>' in html
    # variación diaria al lado del nombre (+0.7% en verde)
    assert '<span class="chg pos">+0.7%</span>' in html
    assert "Reservas" in html and "41,234" in html
    assert "Riesgo País" in html and "750" in html
    # riesgo país subiendo (+15) => clase neg (rojo)
    assert 'class="neg"' in html


class _Argdatos:
    """ArgentinaDatos: corte oficial 1-2×/día (el fallback)."""
    def get_riesgo_pais(self):
        return {"valor": 700, "fecha": "2026-08-06", "delta_abs": 5, "delta_pct": 0.7}


class _BondTerminal:
    def __init__(self, data):
        self._d = data

    def get_riesgo_pais(self):
        return self._d


def test_riesgo_pais_prefiere_bondterminal():
    """BondTerminal refresca cada 5 min contra cotizaciones vivas → gana sobre el corte
    diario de ArgentinaDatos. El número grande sigue siendo el OFICIAL (Ambito/JPM) para
    que la serie sea comparable; el spread ponderado propio va aparte (tooltip)."""
    bt = _BondTerminal({"valor_bps": 448.6, "ambito_bps": 428, "fecha": "2026-08-07",
                        "ambito_delta_1d": -2})
    rp = _riesgo_pais(_Argdatos(), bt)
    assert rp["valor"] == 428 and rp["delta_abs"] == -2
    assert rp["fecha"] == "2026-08-07"
    assert rp["spread_bps"] == 448.6 and rp["fuente"] == "BondTerminal"


def test_riesgo_pais_cae_a_argentinadatos():
    """Sin BondTerminal (caído, sin valor oficial, o directamente ausente) la card
    sigue mostrando el dato de ArgentinaDatos — nunca se queda en blanco."""
    class _Boom:
        def get_riesgo_pais(self):
            raise RuntimeError("bondterminal down")

    for bt in (None, _Boom(), _BondTerminal(None), _BondTerminal({"ambito_bps": None})):
        rp = _riesgo_pais(_Argdatos(), bt)
        assert rp["valor"] == 700, bt
        assert "spread_bps" not in rp


def test_riesgo_pais_none_si_fallan_las_dos_fuentes():
    class _Boom:
        def get_riesgo_pais(self):
            raise RuntimeError("down")

    assert _riesgo_pais(_Boom(), _Boom()) is None


def test_fragment_muestra_el_spread_de_bondterminal_en_el_tooltip():
    html = _TEMPLATES.get_template("fragments/header_cards.html").render(
        dolares=[], reservas=None, reservas_delta=None,
        riesgo_pais={"valor": 428, "fecha": "2026-08-07", "delta_abs": -2,
                     "delta_pct": None, "spread_bps": 448.578, "fuente": "BondTerminal"},
    )
    assert "spread ponderado EMBI AR 449 bps (BondTerminal)" in html
    assert "428" in html


def test_fragment_renders_with_missing_data():
    html = _TEMPLATES.get_template("fragments/header_cards.html").render(
        dolares=[], reservas=None, reservas_delta=None, riesgo_pais=None,
    )
    assert "—" in html  # placeholders, sin romper
