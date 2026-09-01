"""Pata pesos (…O) de ONs hard-dollar → USD por jurisdicción.

Regla de negocio corporativa (may-2026): un corporativo **HARD DOLLAR** que cotiza en
pesos (sufijo …O) se pasa a USD dividiendo por el offer del dólar que corresponde
a su ley: **MEP** (dólar bolsa) si es **Ley Argentina**, **CCL/cable** si es **Ley
Extranjera** — o sin ley declarada (el universo ON es mayormente ley NY). Las patas
…D (MEP) / …C (CABLE) ya cotizan en USD → directo. Los **DOLLAR LINKED** NO entran
acá: son todos locales pero se deflactan por el **oficial** (mayorista/A3500) por la
estructura del bono.
"""

from datetime import date

import pytest

from core.domain.models import Cashflow, Instrument, MarketSnapshot
from core.domain.services import FinancialEngine

# Offers de ejemplo (separados a propósito para detectar cuál se usa).
MAYORISTA, MEP, CCL = 1408.0, 1434.8, 1483.9


class FxStub:
    def get_mayorista_venta(self):  # oficial / A3500
        return MAYORISTA

    def get_mep_venta(self):        # dólar bolsa (casa 'bolsa')
        return MEP

    def get_ccl_venta(self):        # cable (casa 'contadoconliqui')
        return CCL


class IdxStub:
    def get_cer(self, target=None):
        return None


def _hard_dollar(ticker, ley):
    """Bullet hard-dollar cupón 11.875% sem 30/360 (estructura CP38 real), una pata.
    9 cupones futuros de 5.9375 desde 2026-11-28; el último (2030-11-28) con amort 100."""
    coupon_dates = [date(2026, 11, 28), date(2027, 5, 28), date(2027, 11, 28),
                    date(2028, 5, 28), date(2028, 11, 28), date(2029, 5, 28),
                    date(2029, 11, 28), date(2030, 5, 28), date(2030, 11, 28)]
    cfs = [Cashflow(d, 100.0 if d == date(2030, 11, 28) else 0.0, 5.9375)
           for d in coupon_dates]
    return Instrument(
        ticker=ticker, short_name="CGC C38", instrument_type="HARD DOLLAR",
        maturity_date=date(2030, 11, 28), emission_date=date(2025, 11, 28),
        cashflows=tuple(cfs), payment_frequency=2, day_count="30/360",
        category="Obligaciones Negociables", ley_aplicable=ley,
    )


SETTLE = date(2026, 6, 1)
USD_PRICE = 107.9


# --------------------------------------------------------------------------- #
# Clasificación en el modelo
# --------------------------------------------------------------------------- #

def test_is_hard_dollar_and_ley_flags():
    arg = _hard_dollar("CP38O", "Argentina")
    ext = _hard_dollar("CP38O", "Extranjera")
    blank = _hard_dollar("CP38O", None)
    assert arg.is_hard_dollar and not arg.is_dolar_linked
    assert arg.is_ley_argentina
    assert not ext.is_ley_argentina      # Extranjera → no-argentina → CCL
    assert not blank.is_ley_argentina    # sin dato → default CCL


# --------------------------------------------------------------------------- #
# Selección del FX por jurisdicción
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ley,divisor", [
    ("Argentina", MEP),
    ("Extranjera", CCL),
    (None, CCL),            # sin ley → CCL (default ON ≈ ley NY)
    ("extranjera", CCL),    # case-insensitive
])
def test_peso_leg_uses_correct_fx_by_ley(ley, divisor):
    """La pata …O en pesos: TIR == la de un precio USD = peso_price / divisor."""
    fx, idx = FxStub(), IdxStub()
    peso_price = USD_PRICE * divisor
    o_leg = _hard_dollar("CP38O", ley)
    snap_o = MarketSnapshot(instrument=o_leg, price=peso_price)
    tir_o = FinancialEngine.calculate_tir(snap_o, idx, fx, settle_date=SETTLE)

    # Pata …D (USD directo) al precio USD equivalente → debe dar la MISMA TIR.
    d_leg = _hard_dollar("CP38D", ley)
    snap_d = MarketSnapshot(instrument=d_leg, price=USD_PRICE)
    tir_d = FinancialEngine.calculate_tir(snap_d, idx, fx, settle_date=SETTLE)

    assert tir_o == pytest.approx(tir_d, abs=1e-9)


class FxStubAltRates:
    """Mismos métodos, OTROS valores — para probar que …D/…C ignoran el FX."""
    def get_mayorista_venta(self): return 2000.0
    def get_mep_venta(self): return 2500.0
    def get_ccl_venta(self): return 3000.0


def test_usd_legs_are_not_fx_converted():
    """…D y …C ya cotizan en USD → el precio se usa tal cual (sin /FX): la TIR es
    ~9.9% (CP38 @107.9 USD) Y es INDEPENDIENTE de los offers de FX."""
    idx = IdxStub()
    for tk in ("CP38D", "CP38C"):
        leg = _hard_dollar(tk, "Extranjera")
        snap = MarketSnapshot(instrument=leg, price=USD_PRICE)
        tir1 = FinancialEngine.calculate_tir(snap, idx, FxStub(), settle_date=SETTLE)
        tir2 = FinancialEngine.calculate_tir(snap, idx, FxStubAltRates(), settle_date=SETTLE)
        assert tir1 is not None and 0.05 < tir1 < 0.15   # ~9.93% (matchea la referencia)
        assert tir1 == pytest.approx(tir2, abs=1e-12)     # FX no influye en …D/…C


def test_argentina_vs_extranjera_give_different_tir():
    """Misma pata pesos, distinta ley → distinto divisor → distinta TIR
    (MEP 1434.8 ≠ CCL 1483.9)."""
    fx, idx = FxStub(), IdxStub()
    peso_price = 150000.0
    tir_arg = FinancialEngine.calculate_tir(
        MarketSnapshot(instrument=_hard_dollar("CP38O", "Argentina"), price=peso_price),
        idx, fx, settle_date=SETTLE)
    tir_ext = FinancialEngine.calculate_tir(
        MarketSnapshot(instrument=_hard_dollar("CP38O", "Extranjera"), price=peso_price),
        idx, fx, settle_date=SETTLE)
    assert tir_arg is not None and tir_ext is not None
    assert tir_arg != tir_ext
    # CCL > MEP → menor precio USD → mayor TIR para Extranjera.
    assert tir_ext > tir_arg


def test_dollar_linked_still_uses_official_not_mep_ccl():
    """DOLLAR LINKED (no hard-dollar) sigue deflactando por el OFICIAL (mayorista),
    NO por MEP/CCL — la estructura del bono paga pesos × oficial."""
    fx, idx = FxStub(), IdxStub()
    dl = Instrument(
        ticker="CP28O", short_name="CGC C28", instrument_type="DOLLAR LINKED",
        maturity_date=date(2026, 9, 7), emission_date=date(2022, 9, 7),
        cashflows=(Cashflow(date(2026, 6, 7), 33.33, 0.0),
                   Cashflow(date(2026, 9, 7), 33.34, 0.0)),
        payment_frequency=4, day_count="ACT/365", ley_aplicable="Argentina",
    )
    peso_price = 65.3846 * MAYORISTA            # precio que /oficial = 65.3846 USD
    tir_official = FinancialEngine.calculate_tir(
        MarketSnapshot(instrument=dl, price=peso_price), idx, fx, settle_date=SETTLE)
    # Referencia: misma estructura pero pata USD directa @65.3846
    dl_d = dl.model_copy(update={"ticker": "CP28D"})
    tir_ref = FinancialEngine.calculate_tir(
        MarketSnapshot(instrument=dl_d, price=65.3846), idx, fx, settle_date=SETTLE)
    assert tir_official == pytest.approx(tir_ref, abs=1e-9)


# Letras del Tesoro DOLAR_LINKED soberanas (zero-coupon, bullet 100 al vto).
# (ticker, isin, vto, precio_pesos_dirty, usd_implícito, tir_efectiva) — refs de la calculadora
# @ FX mayorista 1446.1064, settle 10/06/2026.
_DL_SOBERANOS = [
    ("D31M7", "AR0637963668", date(2027, 3, 31), 140200.0, 96.95, 0.0392),
    ("D31L6", "AR0769584159", date(2026, 7, 31), 143290.0, 99.0868, 0.0679),
]


@pytest.mark.parametrize("tk,isin,vto,peso,usd,tir_ref", _DL_SOBERANOS,
                         ids=[a[0] for a in _DL_SOBERANOS])
def test_dollar_linked_soberano_matches_referencia(tk, isin, vto, peso, usd, tir_ref):
    """Con el FX de la foto (mayorista 1446.1064) y el precio pesos dirty, settle
    10/06/2026 → USD = pesos/FX, TIR efectiva = la de referencia. Confirma además que es
    FX-dependiente (pata pesos /oficial, no USD directa)."""
    from core.domain.pricing.context import PricingContext
    from core.domain.pricing.strategies import DolarLinkedStrategy

    class FxFoto:
        def get_mayorista_venta(self):
            return 1446.1064

    settle = date(2026, 6, 10)
    inst = Instrument(
        ticker=tk, short_name=f"Letra Tesoro DL {tk}", instrument_type="DOLAR_LINKED",
        maturity_date=vto, emission_date=date(2026, 5, 29), payment_frequency=1,
        day_count="ACT/365.25", cashflows=(Cashflow(vto, 100.0, 0.0),), isin=isin,
    )
    snap = MarketSnapshot(instrument=inst, price=peso)
    tir = FinancialEngine.calculate_tir(snap, IdxStub(), FxFoto(), settle_date=settle)
    assert tir == pytest.approx(tir_ref, abs=1.5e-4), f"{tk} TIR {tir}"
    strat = DolarLinkedStrategy()
    got_usd = strat._usd_price(inst, peso, PricingContext(settle=settle, fx=FxFoto()))
    assert got_usd == pytest.approx(usd, abs=0.01)
    # FX-dependiente: con otro mayorista (1408) la TIR cambia (no es USD-leg directa)
    tir_alt = FinancialEngine.calculate_tir(snap, IdxStub(), FxStub(), settle_date=settle)
    assert tir_alt != pytest.approx(tir, abs=1e-3)


def test_abm_hard_dollar_prefill_routes_to_hard_dollar_strategy():
    """A7: el prefill HARD DOLLAR del form ABM (tipo="HARD DOLLAR") pasa por
    build_instrument y resuelve HardDollarStrategy (NO DolarLinkedStrategy), con la
    dolarización de la pata pesos por MEP/CCL (no invertida). Cierra el bucle del
    prefill end-to-end: los demás tests construyen el Instrument directo."""
    from core.infrastructure.repositories import build_instrument
    from core.domain.pricing.registry import strategy_for
    from core.domain.pricing.strategies import HardDollarStrategy, DolarLinkedStrategy

    fields = {"ticker_ars": "YM34O", "short_name": "YPF", "tipo": "HARD DOLLAR",
              "fecha_emision": "2025-01-17", "fecha_vencimiento": "2034-01-17",
              "cupon anual %": "8.3", "frecuencia pagos": "2", "ley_aplicable": ""}
    inst = build_instrument(fields, "Obligaciones_Negociables",
                            [Cashflow(date(2034, 1, 17), 100.0, 4.15)])
    assert inst.is_hard_dollar and not inst.is_dolar_linked
    strat = strategy_for(inst)
    # HardDollarStrategy hereda de DolarLinkedStrategy → chequear el tipo EXACTO.
    assert isinstance(strat, HardDollarStrategy)
    assert type(strat) is not DolarLinkedStrategy


def test_fx_provider_exposes_mep_ccl_accessors():
    """DolarAPIProvider mapea MEP=bolsa, CCL=contadoconliqui (offer=venta)."""
    from core.infrastructure.fx_provider import DolarAPIProvider
    fx = DolarAPIProvider()
    # Inyectar cache para no pegarle a la red en el test.
    DolarAPIProvider._cache = {
        "bolsa": {"venta": MEP, "compra": 1432.1},
        "contadoconliqui": {"venta": CCL, "compra": 1480.7},
        "mayorista": {"venta": MAYORISTA, "compra": 1399.0},
    }
    import time
    DolarAPIProvider._last_fetch_ts = time.time() + 9999  # evita refetch
    try:
        assert fx.get_mep_venta() == MEP
        assert fx.get_ccl_venta() == CCL
        assert fx.get_mayorista_venta() == MAYORISTA
    finally:
        DolarAPIProvider._cache = {}
        DolarAPIProvider._last_fetch_ts = 0.0
