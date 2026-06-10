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
    keys = store.keys()

    def _matching(fondo):
        base = norm(fondo)
        if not base:
            return []
        return [k for k in keys if k == base or k.startswith(base + " - ")]

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

    if not force:
        with _LOCK:
            if _CACHE["key"] == key and _CACHE["data"] is not None:
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
    with _LOCK:
        _CACHE["key"], _CACHE["data"] = key, ds
    return ds


def clear_cache() -> None:
    with _LOCK:
        _CACHE["key"], _CACHE["data"] = None, None
