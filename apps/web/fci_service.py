"""Servicio del panel FCI: junta las fuentes (CAFCI enriquecido + AUM ArgentinaDatos +
macro real + flujos/hist de `fci_history`) y arma el dataset que sirve `/fci/data`,
memoizado por (corte CAFCI, día). Aísla al provider de indices/fx (el provider solo parsea).
"""
from __future__ import annotations

import threading
from datetime import date

from core.domain.fci.dataset import build_fci_dataset
from core.domain.fci.derive import build_aum_index, norm
from core.domain.fci.lens import compute_macro
from core.infrastructure.fci_history import (
    fetch_ard_fci_rows, get_fci_history_store, net_flow_series,
)

_CACHE = {"key": None, "data": None}
_LOCK = threading.Lock()


def _store_lookups(store):
    """flows_lookup(fondo)->{date:flow} y hist_lookup(fondo)->{date:{vcp,..}} agregando las
    clases de cada fondo (las claves del store son nombres de clase de ArgentinaDatos)."""
    # Índice base -> claves, construido UNA vez. Antes `_matching` recorría las ~4.900
    # claves del store por cada uno de los ~1.100 fondos (5,4 M comparaciones de string,
    # y se llamaba dos veces por fondo): ~1.012 ms por build del dataset. Con el índice
    # baja a ~2,5 ms (205×) y construirlo cuesta 2,1 ms. Resultado verificado idéntico
    # (mismo orden) sobre los 1.096 fondos reales.
    _idx: dict = {}
    for k in store.keys():
        _idx.setdefault(k, []).append(k)
        b = k.split(" - ", 1)[0]      # "Fondo - Clase A" -> "Fondo"
        if b != k:
            _idx.setdefault(b, []).append(k)

    def _matching(fondo):
        base = norm(fondo)
        if not base:
            return []
        return _idx.get(base, [])

    def flows_lookup(fondo):
        merged = {}
        for k in _matching(fondo):
            for d, flow in net_flow_series(store.get_series(k)).items():
                merged[d] = merged.get(d, 0.0) + flow
        return merged

    def hist_lookup(fondo):
        best = {}
        for k in _matching(fondo):       # clase principal ≈ la serie más larga
            s = store.get_series(k)
            if len(s) > len(best):
                best = s
        return best

    return flows_lookup, hist_lookup


def get_fci_dataset(cafci, indices=None, fx=None, *, force: bool = False) -> dict:
    """Dataset `{meta, funds}` para el panel. Memoizado por (generated_at del corte CAFCI, hoy)."""
    cafci._ensure_loaded()
    parsed = getattr(cafci, "_dataset", None) or {"meta": {}, "funds": []}
    meta = parsed.get("meta", {})
    fecha_base, generated_at = meta.get("fecha_base"), meta.get("generated_at")
    key = (generated_at, str(date.today()))

    # SINGLE-FLIGHT: el build entero corre DENTRO del lock. El double-checked locking
    # anterior solo protegía la lectura/escritura del cache, así que N requests en
    # cache frío (restart, primera carga del día, el usuario recargando impaciente)
    # disparaban N builds y N×5 GETs a ArgentinaDatos. El build es idempotente: con el
    # lock tomado, el segundo request espera y se lleva el resultado del primero.
    # Con el índice de _store_lookups el hold bajó a ~800 ms.
    with _LOCK:
        if not force and _CACHE["key"] == key and _CACHE["data"] is not None:
            return _CACHE["data"]

        aum_index = build_aum_index(fetch_ard_fci_rows())
        cer_series = indices.cer_series() if indices else {}
        a3500_series = indices.a3500_series() if indices else {}
        mep_now = fx.get_mep_venta() if fx else None
        macro = compute_macro(cer_series, a3500_series, mep_now, fecha_base)

        flows_lookup, hist_lookup = _store_lookups(get_fci_history_store())
        ds = build_fci_dataset(parsed.get("funds", []), aum_index, macro,
                               flows_lookup=flows_lookup, hist_lookup=hist_lookup,
                               fecha_base=fecha_base, generated_at=generated_at)
        # NO memoizar un dataset degradado: si ArgentinaDatos falló, `aum_index` viene
        # vacío y cachearlo dejaba el panel sin AUM hasta la medianoche siguiente por
        # una caída transitoria. Se devuelve igual (mejor eso que nada) pero se
        # reintenta en la próxima visita.
        if aum_index:
            _CACHE["key"], _CACHE["data"] = key, ds
    return ds


def clear_cache() -> None:
    with _LOCK:
        _CACHE["key"], _CACHE["data"] = None, None
