"""Servicio del panel FCI: junta las fuentes (CAFCI enriquecido + AUM ArgentinaDatos +
macro real + flujos/hist de `fci_history`) y arma el dataset que sirve `/fci/data`,
memoizado por (corte CAFCI, día). Aísla al provider de indices/fx (el provider solo parsea).
"""
from __future__ import annotations

import logging
import threading
from datetime import date

from core.domain.fci.dataset import build_fci_dataset
from core.domain.fci.derive import build_aum_index, ccy, norm
from core.domain.fci.lens import compute_macro
from core.infrastructure.fci_history import (
    fetch_ard_fci_rows, get_fci_history_store, net_flow_series,
)

logger = logging.getLogger(__name__)

_CACHE = {"key": None, "data": None}
_LOCK = threading.Lock()


def _ccy_index(parsed_funds):
    """`(por_clase, por_fondo)`: la moneda ('ARS'/'USD') de cada CLASE de CAFCI y, cuando
    todas las clases de un fondo comparten moneda, la del fondo.

    Es el insumo para separar los flujos POR MONEDA DE CLASE antes de mergearlos: 95 de
    los 1.096 fondos del corte real tienen clases en monedas distintas (10 de ellos
    quedan rotulados 'USD' a nivel fondo teniendo clases en pesos), así que convertir el
    merge con la moneda del FONDO multiplica pesos por el MEP. `por_fondo` es None para
    esos fondos mixtos: sus claves de store sin match no se pueden resolver y caen al
    default conservador (ARS) en `flows_lookup`.
    """
    by_class: dict = {}
    by_fondo: dict = {}
    for rec in parsed_funds or ():
        c = ccy(rec.get("moneda"))
        clase, fondo = rec.get("clase_nombre"), rec.get("fondo_nombre")
        for k in (norm(clase), norm(f"{fondo} - {clase}") if fondo and clase else ""):
            if k:
                by_class[k] = c
        fk = norm(fondo)
        if fk:
            by_fondo[fk] = c if by_fondo.get(fk, c) == c else None
    return by_class, by_fondo


def _store_lookups(store, by_class=None, by_fondo=None):
    """flows_lookup(fondo)->{moneda:{date:flow}} y hist_lookup(fondo)->{date:{vcp,..}}
    agregando las clases de cada fondo (las claves del store son nombres de clase de
    ArgentinaDatos).

    Los flujos se agrupan **por la moneda de la CLASE**, no por la del fondo: nacen en la
    moneda nativa de cada clase (Δccp × precio de cuotaparte) y mergearlos antes de
    convertir sumaba pesos con dólares. Caso medido sobre el corte real: 'Alamerica Renta
    Fija Argentina' (rotulado USD) tiene su Clase I en pesos con +3,124e9 de flujo; al
    convertir el merge con la moneda del fondo quedaba en +4,374e12 (×1400 el MEP), que
    por sí solo daba vuelta el signo del agregado del mercado.
    """
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

    by_class = by_class or {}
    by_fondo = by_fondo or {}
    stats = {"keys": 0, "unresolved": 0}

    def _matching(fondo):
        base = norm(fondo)
        if not base:
            return []
        return _idx.get(base, [])

    def flows_lookup(fondo):
        merged: dict = {}
        # Fondo mono-moneda -> esa moneda cubre también las clases que CAFCI no publica
        # (7,2% de las claves del store). Fondo mixto -> None, y ahí el default es ARS:
        # subestimar una clase en dólares (÷1400) es un error acotado; sobreestimar una
        # en pesos (×1400) es el que hacía explotar el agregado.
        default = by_fondo.get(norm(fondo)) or "ARS"
        for k in _matching(fondo):
            moneda = by_class.get(k)
            stats["keys"] += 1
            if moneda is None:
                stats["unresolved"] += 1
                moneda = default
            bucket = merged.setdefault(moneda, {})
            for d, flow in net_flow_series(store.get_series(k)).items():
                bucket[d] = bucket.get(d, 0.0) + flow
        return merged

    def hist_lookup(fondo):
        best = {}
        for k in _matching(fondo):       # clase principal ≈ la serie más larga
            s = store.get_series(k)
            if len(s) > len(best):
                best = s
        return best

    return flows_lookup, hist_lookup, stats


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

        by_class, by_fondo = _ccy_index(parsed.get("funds", []))
        flows_lookup, hist_lookup, stats = _store_lookups(
            get_fci_history_store(), by_class, by_fondo)
        ds = build_fci_dataset(parsed.get("funds", []), aum_index, macro,
                               flows_lookup=flows_lookup, hist_lookup=hist_lookup,
                               fecha_base=fecha_base, generated_at=generated_at)
        if stats["unresolved"]:
            logger.info("fci: %d/%d claves del store de flujos sin moneda de clase "
                        "(default por fondo/ARS)", stats["unresolved"], stats["keys"])
        # NO memoizar un dataset degradado: si ArgentinaDatos falló, `aum_index` viene
        # vacío y cachearlo dejaba el panel sin AUM hasta la medianoche siguiente por
        # una caída transitoria. Ídem `macro['mep_now']`: sin MEP el panel deshabilita
        # el lente 'USD @MEP' de la cuotaparte y saca del agregado de Flujos a los
        # fondos con clases en dólares (con aviso), así que congelar un build hecho con
        # el cache de FX frío degradaba la vista TODO el día aunque dolarapi volviera a
        # los 5 minutos. Se devuelve igual (mejor eso que nada) pero se reintenta en la
        # próxima visita.
        if aum_index and macro.get("mep_now"):
            _CACHE["key"], _CACHE["data"] = key, ds
    return ds


def clear_cache() -> None:
    with _LOCK:
        _CACHE["key"], _CACHE["data"] = None, None
