"""La revisión sólo sube si cambió algo que se MUESTRA.

Medido con `scripts/bench_churn.py` y el mercado abierto (2026-09-04): al intervalo de
producción (5 s) se mueve el precio del **1,2%** de los instrumentos y algún campo del
**6%**; fuera de rueda —el 75% del tiempo— no se mueve nada. Y sin embargo cada ciclo
despertaba a todos los clientes SSE, que disparan ~14 `hx-get` cada uno, y cada
fragmento abre una sesión de SQLite para resolver los permisos de pestaña.

La frescura NO depende de esto: `_last_refresh`, el badge y `is_stale` se actualizan
en todos los ciclos. Lo único que se gatea es el aviso a los clientes.
"""

import asyncio

from core.domain.models import Instrument, InstrumentMetrics, MarketSnapshot

from apps.web.state import AppState


def _m(ticker="AL30", price=100.0, tir=0.15, volume=1000.0, **kw):
    inst = Instrument(ticker=ticker, short_name=ticker, instrument_type="BONAR")
    snap = MarketSnapshot(instrument=inst, price=price, volume=volume,
                          **{k: v for k, v in kw.items() if k in
                             ("bid", "ask", "operations", "change_pct")})
    return InstrumentMetrics(snapshot=snap, tir=tir,
                             **{k: v for k, v in kw.items() if k.startswith("variance")
                                or k in ("duration", "technical_value", "parity")})


def _correr(pasos):
    """Aplica una lista de updates y devuelve la revisión después de cada uno."""
    async def run():
        st = AppState()
        out = []
        for metrics in pasos:
            await st.update(metrics)
            out.append(st.revision)
        return out
    return asyncio.run(run())


def test_el_primer_update_siempre_notifica():
    """Aunque venga vacío: si no, un arranque sin datos deja a los clientes SSE
    esperando para siempre."""
    assert _correr([[]])[0] == 1


def test_dos_ciclos_identicos_no_bumpean():
    revs = _correr([[_m()], [_m()]])
    assert revs[0] == revs[1], (
        "la revisión subió con métricas idénticas: cada cliente SSE va a refetchear "
        "sus ~14 fragmentos para ver exactamente lo mismo")


def test_la_frescura_se_actualiza_igual():
    """El gateo es del aviso a los clientes, NO del estado: si `_last_refresh` no
    avanzara, `/api/health` diría `is_stale` con datos frescos y el badge se pondría
    en rojo solo."""
    async def run():
        st = AppState()
        await st.update([_m()])
        primero = st.status(stale_after_s=30)["last_refresh"]
        await asyncio.sleep(0.01)
        await st.update([_m()])             # idéntico → no notifica
        return primero, st.status(stale_after_s=30)
    primero, luego = asyncio.run(run())
    assert luego["last_refresh"] != primero, "`_last_refresh` no avanzó"
    assert luego["is_stale"] is False


def test_wait_for_change_sigue_bloqueando_si_no_cambio_nada():
    """La contracara: un cliente conectado no puede quedar colgado por el gateo, pero
    tampoco puede despertarse sin motivo."""
    async def run():
        st = AppState()
        await st.update([_m()])
        rev = st.revision
        await st.update([_m()])             # idéntico
        try:
            await asyncio.wait_for(st.wait_for_change(rev), timeout=0.05)
            return "desperto"
        except asyncio.TimeoutError:
            return "bloqueado"
    assert asyncio.run(run()) == "bloqueado"


def test_un_precio_distinto_bumpea():
    revs = _correr([[_m(price=100.0)], [_m(price=101.0)]])
    assert revs[1] > revs[0]


def test_un_volumen_distinto_TAMBIEN_bumpea():
    """La decisión de granularidad, explícita: la huella lleva TODO campo mostrado, no
    sólo las métricas. `Vol $` y `Var%` se muestran en los paneles y se mueven con cada
    operación aunque el precio no cambie; con una clave sólo de métricas quedarían
    stale entre bumps. Cuesta más bumps (6% vs 1,2% de churn) y es lo correcto."""
    revs = _correr([[_m(volume=1000.0)], [_m(volume=2000.0)]])
    assert revs[1] > revs[0], "el volumen cambió y los paneles lo muestran"


def test_una_tir_distinta_bumpea():
    revs = _correr([[_m(tir=0.15)], [_m(tir=0.16)]])
    assert revs[1] > revs[0]


def test_una_variance_distinta_bumpea():
    revs = _correr([[_m(variance_30d=0.05)], [_m(variance_30d=0.09)]])
    assert revs[1] > revs[0]


def test_un_ticker_nuevo_bumpea():
    revs = _correr([[_m("AL30")], [_m("AL30"), _m("GD30")]])
    assert revs[1] > revs[0]


def test_un_ticker_que_desaparece_bumpea():
    """Un bono que deja de cotizar sale de los paneles: el cliente tiene que enterarse."""
    revs = _correr([[_m("AL30"), _m("GD30")], [_m("AL30")]])
    assert revs[1] > revs[0]


def test_el_memo_del_dataset_de_ON_sobrevive_un_ciclo_sin_cambios():
    """`on_service` memoiza por `(revision, día)`. Mientras la revisión subía en cada
    ciclo, ese memo se invalidaba cada 5 s y el primer `/on/data` de cada ciclo
    reconstruía el dataset entero. Con el gateo, sobrevive gratis."""
    from apps.web import on_service

    async def run():
        st = AppState()
        await st.update([_m()])
        primero = on_service.get_on_dataset(st)
        await st.update([_m()])             # idéntico
        return primero, on_service.get_on_dataset(st)

    a, b = asyncio.run(run())
    assert a is b, "el memo del dataset de ON se invalidó sin que cambiara nada"
