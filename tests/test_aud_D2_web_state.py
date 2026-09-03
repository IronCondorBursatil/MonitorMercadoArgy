"""Auditoría D2 — `AppState`: el éxito de UN loop no puede borrar la caída de OTRO.

`_error` era el canal COMPARTIDO por los cinco loops supervisados y `update()` —que
corre cada `refresh_sec` (5s) desde el refresh loop— lo limpiaba incondicionalmente:
la marca de caída de cualquier loop vivía a lo sumo un ciclo de refresh, mucho menos
que los 15s de polling del badge del header, así que no se veía nunca.

Hoy cada caída va a su canal PROPIO (`_loop_crashes`, que `update()` no toca) y la
SEVERIDAD la da el loop: sólo la caída del refresh loop —el que produce el snapshot—
marca `last_error` (badge rojo). La de un loop lateral es degradación parcial y se ve
en `loop_crashes`/`degraded_loops` — ver tests/test_rem_R3_web_state_severity.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from apps.web.state import AppState

_CRASH = "loop ratings cayó (CancelledError espuria (no venía del shutdown)) — reiniciando"


def test_un_refresh_exitoso_no_borra_la_caida_de_otro_loop():
    """El invariante del hallazgo, en el canal que hoy lo sostiene: el ciclo bueno
    del refresh loop NO puede borrar el rastro de que se cayó el de ratings."""
    async def run():
        st = AppState()
        await st.record_error(_CRASH)      # cae el loop de ratings (03:00 AM)
        await st.update([])                # ≤5s después: ciclo normal del refresh
        s = st.status(stale_after_s=30)
        assert [c["loop"] for c in s["loop_crashes"]] == ["ratings"], \
            "el éxito del refresh loop borró la caída del loop de ratings"
        assert s["degraded_loops"] == ["ratings"]
        # ...pero un loop LATERAL no apaga el semáforo de los precios: pintar el
        # badge de rojo ("sin datos") con el snapshot de hace 5s era la regresión
        # que introdujo la retención de 300s sobre el canal compartido.
        assert s["last_error"] is None and s["ok"] is True
    asyncio.run(run())


def test_la_caida_del_refresh_loop_sobrevive_al_ciclo_siguiente():
    """La mitad crítica del mismo invariante: el loop que produce el snapshot SÍ
    marca el badge, y la marca dura más que el ciclo de 5s (el badge poll-ea a 15s)."""
    async def run():
        st = AppState()
        await st.record_loop_crash("refresh", "CancelledError espuria")
        await st.update([])
        s = st.status(stale_after_s=30)
        assert s["last_error"] and "refresh" in s["last_error"]
        assert s["ok"] is False
    asyncio.run(run())


def test_la_caida_queda_registrada_por_loop_en_status():
    """Canal propio con retención: aunque el marcador del badge caduque, el registro
    de qué loop se cayó y cuándo sigue disponible para health/diagnóstico."""
    async def run():
        st = AppState()
        await st.record_loop_crash("price_history", "CancelledError espuria")
        await st.update([])
        crashes = st.status(stale_after_s=30)["loop_crashes"]
        assert [c["loop"] for c in crashes] == ["price_history"]
        assert "CancelledError" in crashes[0]["reason"]
        assert crashes[0]["at"]
    asyncio.run(run())


def test_el_marcador_de_caida_caduca_y_no_queda_pegado_para_siempre():
    """No es sticky eterno: pasada la ventana de retención un refresh sano vuelve a
    dejar el badge verde (pero el registro por loop sigue)."""
    async def run():
        st = AppState(crash_sticky_s=0.0)
        await st.record_loop_crash("refresh", "boom")   # el único que marca badge
        await st.update([])
        s = st.status(stale_after_s=30)
        assert s["last_error"] is None
        assert s["ok"] is True
        assert [c["loop"] for c in s["loop_crashes"]] == ["refresh"]
        assert s["degraded_loops"] == []   # fuera de la ventana de retención
    asyncio.run(run())


def test_el_error_del_propio_ciclo_de_refresh_lo_sigue_limpiando_el_exito():
    """Contrato existente intacto: un blip del refresh se limpia con el ciclo bueno
    siguiente (sólo las caídas de loops son las que sobreviven)."""
    async def run():
        st = AppState()
        await st.record_error("HTTPError: data912 timeout")
        await st.update([])
        s = st.status(stale_after_s=30)
        assert s["last_error"] is None
        assert s["ok"] is True
        assert s["loop_crashes"] == []
    asyncio.run(run())


def test_las_caidas_viejas_se_purgan_del_registro():
    async def run():
        st = AppState()
        await st.record_loop_crash("options", "boom")
        # envejecer la entrada más allá de la ventana de retención
        msg, _at = st._loop_crashes["options"]
        st._loop_crashes["options"] = (msg, datetime.now() - timedelta(days=2))
        assert st.status(stale_after_s=30)["loop_crashes"] == []
    asyncio.run(run())
