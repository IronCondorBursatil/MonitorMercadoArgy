"""Alias de precio: un instrumento que cotiza con la cotización de OTRO símbolo.

Caso real: `TY30PUT` es el BONTE 2030 (`TY30P`) con la opción de venta ejercida el
2027-05-30 —mismo papel, otro schedule— y ni BYMA ni Data912 listan un símbolo para
esa variante (eldashboard la muestra como TY30PP con otra fuente). La fila se cargó
con `raw_fields["precio_fallback"] = "precio_de:TY30P"`, pero ningún código leía esa
clave: cero precios desde el alta, fila muerta en el panel Tasa Fija.

El contrato que fija este archivo:
  · `Instrument.price_alias` sale de `raw_fields["precio_fallback"]` (`precio_de:<T>`).
  · El use-case pide también la cotización del alias y, si el ticker propio no cotiza,
    precia con una COPIA del snapshot del alias (cada bono lleva su `instrument`).
  · El histórico (variaciones) también se lee del alias.
  · El popup de detalle hace lo mismo.
"""

from datetime import timedelta

from core.domain.models import Cashflow, Instrument, MarketSnapshot
from core.infrastructure.db.catalog_repository import _orm_to_domain
from core.infrastructure.db.models import InstrumentORM
from core.use_cases import generate_report as gr
from tests._clock import ref_date


# ── del blob del ABM al dominio ──────────────────────────────────────────────
def _orm(**raw):
    return InstrumentORM(ticker="TY30PUT", short_name="TY30PUT", instrument_type="BONOFIJA",
                         day_count="ACT/365.25", cer_lag=10, payment_frequency=2,
                         raw_fields=raw or None)


def test_precio_fallback_precio_de_se_lee_como_price_alias():
    assert _orm_to_domain(_orm(precio_fallback="precio_de:TY30P")).price_alias == "TY30P"


def test_el_alias_se_normaliza_a_mayusculas_sin_espacios():
    assert _orm_to_domain(_orm(precio_fallback=" precio_de: ty30p ")).price_alias == "TY30P"


def test_sin_la_clave_o_con_otra_forma_no_hay_alias():
    assert _orm_to_domain(_orm()).price_alias is None
    assert _orm_to_domain(_orm(precio_fallback="TY30P")).price_alias is None
    assert _orm_to_domain(_orm(precio_fallback="precio_de:")).price_alias is None


# ── el use-case ──────────────────────────────────────────────────────────────
def _bono(ticker, alias=None):
    ref = ref_date()
    mat = ref + timedelta(days=365)
    return Instrument(
        ticker=ticker, short_name=ticker, instrument_type="BONOFIJA",
        maturity_date=mat, emission_date=ref - timedelta(days=365),
        cashflows=(Cashflow(date=mat, amortization=100.0, interest=14.75),),
        price_alias=alias,
    )


class _Prov:
    def __init__(self, cotizan):
        self.cotizan = cotizan
        self.pedidos = []
        self.historicos = []

    def fetch_snapshots(self, tickers):
        self.pedidos.append(list(tickers))
        return {t: MarketSnapshot(instrument=None, price=px)
                for t, px in self.cotizan.items() if t in tickers}

    def fetch_historical_prices(self, t, d):
        self.historicos.append(t)
        return {}


def _use_case(monkeypatch, instrumentos, prov):
    class _Idx:
        def get_cer(self, *a, **k):
            return None

    class _Fx:
        def get_quote(self, k):
            return None

    monkeypatch.setattr(gr, "BCRAIndicesProvider", lambda **k: _Idx())
    monkeypatch.setattr(gr, "DolarAPIProvider", lambda: _Fx())
    gr._HIST_BASE_CACHE.clear()

    class _Repo:
        def get_instruments_by_type(self, t):
            return list(instrumentos) if t == "BONOFIJA" else []

    return gr.GenerateMonitorReport(_Repo(), prov)


def test_el_alias_precia_con_el_precio_del_otro_simbolo_y_su_propio_schedule(monkeypatch):
    prov = _Prov({"TY30P": 95.0})
    uc = _use_case(monkeypatch, [_bono("TY30P"), _bono("TY30PUT", alias="TY30P")], prov)
    por_ticker = {m.snapshot.instrument.ticker: m for m in uc.execute(["BONOFIJA"])}

    assert set(por_ticker) == {"TY30P", "TY30PUT"}
    assert por_ticker["TY30PUT"].snapshot.price == 95.0
    assert por_ticker["TY30PUT"].tir is not None
    # Cada bono lleva SU instrument: el snapshot del alias es una copia, no el mismo
    # objeto (sino el último en asignar `instrument` pisaría al otro).
    assert por_ticker["TY30P"].snapshot is not por_ticker["TY30PUT"].snapshot
    assert por_ticker["TY30P"].snapshot.instrument.ticker == "TY30P"


def test_el_alias_se_pide_al_provider_aunque_no_este_en_los_tipos_pedidos(monkeypatch):
    """El símbolo del que se toma el precio puede no ser parte del panel (o no ser
    un instrumento del catálogo): hay que pedirlo igual."""
    prov = _Prov({"TY30P": 95.0})
    uc = _use_case(monkeypatch, [_bono("TY30PUT", alias="TY30P")], prov)
    metrics = uc.execute(["BONOFIJA"])
    assert [m.snapshot.instrument.ticker for m in metrics] == ["TY30PUT"]
    assert "TY30P" in prov.pedidos[0]
    # Las variaciones (Sem/1M/3M/YTD/1A) también salen del histórico del alias.
    assert prov.historicos == ["TY30P"]


def test_el_ticker_propio_gana_si_cotiza(monkeypatch):
    prov = _Prov({"TY30P": 95.0, "TY30PUT": 90.0})
    uc = _use_case(monkeypatch, [_bono("TY30PUT", alias="TY30P")], prov)
    assert uc.execute(["BONOFIJA"])[0].snapshot.price == 90.0


def test_sin_cotizacion_del_alias_el_bono_se_saltea_sin_romper(monkeypatch):
    prov = _Prov({})
    uc = _use_case(monkeypatch, [_bono("TY30PUT", alias="TY30P")], prov)
    assert uc.execute(["BONOFIJA"]) == []


# ── el popup de detalle ──────────────────────────────────────────────────────
class _RepoDetalle:
    def __init__(self, *instrumentos):
        self._por_ticker = {i.ticker: i for i in instrumentos}

    def get_instrument_by_ticker(self, t):
        return self._por_ticker.get(t)


class _Idx:
    def get_cer(self, target=None):
        return None

    def get_tamar(self, target=None):
        return None


class _Fx:
    def get_mayorista_venta(self):
        return 1100.0


def test_el_popup_muestra_el_precio_del_alias():
    from apps.web import bond_detail

    prov = _Prov({"TY30P": 95.0})
    d = bond_detail.get_bond_detail("TY30PUT", _RepoDetalle(_bono("TY30PUT", alias="TY30P")),
                                    prov, _Idx(), _Fx(), settlement_lag=1)
    assert d is not None and d["ticker"] == "TY30PUT"
    assert d["market"]["price"] == 95.0
    assert d["metrics"]["tir"] is not None


def test_la_calculadora_del_popup_tambien_resuelve_el_alias():
    from apps.web import bond_detail

    prov = _Prov({"TY30P": 95.0})
    r = bond_detail.calculate("TY30PUT", _RepoDetalle(_bono("TY30PUT", alias="TY30P")),
                              prov, _Idx(), _Fx(), mode="from_price", price=95.0)
    assert r is not None
    assert "TY30P" in prov.pedidos[-1]
