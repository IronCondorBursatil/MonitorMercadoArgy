"""Servicio del panel O.N's: arma el dataset de `/on/data` desde el snapshot vivo
(`AppState.metrics()`), clasificando por sector al vuelo. Memoizado por (revisión del
snapshot, día) — espejo de `fci_service`. El shape es idéntico al `window.ON_DATA` que
consume el front (static/js/on.js, porteado del mock 21), así que el cliente no cambia.

Escalas: las métricas guardan `tir`/`parity` en decimales → ×100 para %; `change_pct`
viene de Data912 ya en escala % (no se multiplica); `md`/`price`/`vtec`/`volume` van tal cual.
"""
from __future__ import annotations

import threading
from datetime import date, datetime

from apps.web.routers.panels import _ley_of, _ticker_ccy
from core.domain.instrument_groups import OBLIGACIONES_NEGOCIABLES
from core.domain.on_classification import SECTORS, sector_for
from core.domain.services import FinancialEngine

_ON_TYPES = set(OBLIGACIONES_NEGOCIABLES)
_FX_NOTE = "Pata pesos (…O) valuada MEP (ley AR) / CCL (ley EXT). …D=MEP, …C=CABLE."

_CACHE = {"key": None, "data": None}
_LOCK = threading.Lock()


def _pct100(v):
    return v * 100 if v is not None else None


def _peso_fx_rate(inst, fx):
    """FX para pasar la pata pesos (…O) a USD, igual que el motor (strategies.py):
    hard-dollar → MEP (ley AR) / CCL (ley EXT); dollar-linked → mayorista (oficial).
    None si no hay provider/método (degradación segura)."""
    if fx is None:
        return None
    if inst.is_hard_dollar:
        meth = "get_mep_venta" if inst.is_ley_argentina else "get_ccl_venta"
    elif inst.is_dolar_linked:
        meth = "get_mayorista_venta"
    else:
        return None
    fn = getattr(fx, meth, None)
    try:
        rate = fn() if fn else None
    except Exception:  # noqa: BLE001 — provider caído → sin conversión
        return None
    return rate if (rate and rate > 0) else None


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def _current_coupon(inst, today):
    """Cupón VIGENTE (TNA %) = interés del próximo flujo / fracción de año de su período.
    Para bonos regulares = la tasa fija; para STEP-UPS (ej. CLSIO) = la tasa del tramo
    actual. Fallback a coupon_rate (raw_fields) si no hay flujo futuro con interés."""
    future = [cf for cf in inst.cashflows if cf.date and cf.date > today and cf.interest > 0]
    if not future:
        return inst.coupon_rate
    nxt = future[0]
    prevs = [cf.date for cf in inst.cashflows if cf.date and cf.date < nxt.date]
    start = max(prevs) if prevs else inst.emission_date
    if not start:
        return inst.coupon_rate
    try:
        dcf = inst.year_fraction_to(nxt.date, start)
    except Exception:  # noqa: BLE001
        return inst.coupon_rate
    return round(nxt.interest / dcf, 2) if (dcf and dcf > 0) else inst.coupon_rate


def _build_on_dataset(state, fx=None) -> dict:
    today = date.today()
    bonds = []
    for m in state.metrics():
        snap = m.snapshot
        inst = snap.instrument if snap else None
        if not inst or inst.instrument_type not in _ON_TYPES:
            continue
        if not snap.price:          # PRICE_REQUIRED: una pata sin cotización no genera fila
            continue
        vto = inst.maturity_date
        # próximo cupón = primer flujo futuro (los cashflows vienen ordenados por fecha)
        prox = next((cf.date for cf in inst.cashflows if cf.date and cf.date > today), None)
        # métricas de display (reusan las funciones puras del motor, como el popup bond_detail).
        # Current Yield = cupón/clean: precio y flujos deben estar en la misma moneda. Las patas
        # USD (MEP/CABLE) ya cotizan en USD; la pata ARS se pasa a USD dividiendo el precio en
        # pesos por MEP (ley local) / cable (ley extranjera), igual que el motor. Convexidad no
        # usa el precio de mercado → vale en todas las patas.
        ccy = _ticker_ccy(inst.ticker)
        price_usd = snap.price
        if ccy == "ARS" and snap.price:
            rate = _peso_fx_rate(inst, fx)
            price_usd = (snap.price / rate) if rate else None
        cy = FinancialEngine.current_yield(inst, price_usd, today) if price_usd else None
        convex = FinancialEngine.convexity(inst, m.tir, today) if m.tir is not None else None
        bonds.append({
            "ticker": inst.ticker,
            "ccy": ccy,
            "ley": _ley_of(inst),
            "tipo": "HD" if inst.is_hard_dollar else "DL",  # Hard Dollar vs Dollar Linked
            "emisor": inst.short_name,
            "clase": inst.serie_clase,        # "Serie / Clase" del ABM (raw_fields.serie_clase)
            "sector": sector_for(inst),       # override manual (ABM) o derivado del emisor
            "emision": inst.emission_date.isoformat() if inst.emission_date else None,
            "vto": vto.isoformat() if vto else None,
            "cupon": _current_coupon(inst, today),   # cupón vigente (step-up → tramo actual)
            "frec": inst.payment_frequency,   # cupones por año (2=sem, 4=trim, 1=anual, 12=mens)
            "dias": (vto - today).days if vto else None,
            "prox_cupon": prox.isoformat() if prox else None,
            "dias_cupon": (prox - today).days if prox else None,  # días al próximo cupón
            "price": snap.price,
            "vtec": m.technical_value,
            "paridad": _pct100(m.parity),
            "tir": _pct100(m.tir),
            "cy": _pct100(cy),                # current yield %
            "md": m.duration,
            "convex": round(convex, 2) if convex is not None else None,  # convexidad (años²)
            "change_pct": snap.change_pct,   # ya en escala % (Data912 pct_change)
            "volume": snap.volume,
        })

    # Resúmenes por sector sobre la pata MEP (instrumento canónico, 1 por ON).
    mep = [b for b in bonds if b["ccy"] == "MEP"]
    by_sector: dict = {}
    for b in mep:
        by_sector.setdefault(b["sector"], []).append(b)
    sectors = sorted(
        ({
            "key": s, "label": s, "count": len(rows),
            "tir_avg": _avg([r["tir"] for r in rows]),
            "md_avg": _avg([r["md"] for r in rows]),
            "n_ar": sum(1 for r in rows if r["ley"] == "AR"),
            "n_ext": sum(1 for r in rows if r["ley"] == "EXT"),
        } for s, rows in by_sector.items()),
        key=lambda x: -x["count"],
    )
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "bonds": bonds,
        "sectors": sectors,
        "sectors_meta": [
            {"key": s.key, "short": s.short, "color": s.color, "icon": s.icon}
            for s in SECTORS
        ],
        "meta": {
            "n_bonds": len(mep),
            "n_ar": sum(1 for b in mep if b["ley"] == "AR"),
            "n_ext": sum(1 for b in mep if b["ley"] == "EXT"),
            "n_legs": len(bonds),
            "ccys": ["ARS", "MEP", "CABLE"],
            "fx_note": _FX_NOTE,
        },
    }


def get_on_dataset(state, fx=None, *, force: bool = False) -> dict:
    """Dataset `{generated, today, bonds, sectors, meta}` memoizado por (revisión, hoy).
    `fx` (FxProvider) se usa para pasar a USD la pata pesos en el Current Yield."""
    key = (state.revision, str(date.today()))
    if not force:
        with _LOCK:
            if _CACHE["key"] == key and _CACHE["data"] is not None:
                return _CACHE["data"]
    ds = _build_on_dataset(state, fx)
    with _LOCK:
        _CACHE["key"], _CACHE["data"] = key, ds
    return ds


def clear_cache() -> None:
    with _LOCK:
        _CACHE["key"], _CACHE["data"] = None, None
