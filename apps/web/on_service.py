"""Servicio del panel O.N's: arma el dataset de `/on/data` desde el snapshot vivo
(`AppState.metrics()`), clasificando por sector al vuelo. Memoizado por (revisión del
snapshot, día) — espejo de `fci_service`. El shape es idéntico al `window.ON_DATA` que
consume el front (static/js/on.js, porteado del mock 21), así que el cliente no cambia.

Escalas: las métricas guardan `tir`/`parity` en decimales → ×100 para %; `change_pct`
viene de Data912 ya en escala % (no se multiplica); `md`/`price`/`vtec`/`volume` van tal cual.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime

from apps.web.panels_rows import _ley_of
from core.domain.currency import ccy_from_suffix as _ticker_ccy
from core.domain.instrument_groups import OBLIGACIONES_NEGOCIABLES
from core.domain.on_classification import SECTORS, sector_for
from core.domain.pricing.fx_legs import peso_leg_to_usd
from core.domain.services import FinancialEngine
from core.infrastructure import ratings as _ratings
from core.infrastructure import ratings_history as _rhist
from core.infrastructure.ratings import SOURCE as _CAL_SRC
from core.infrastructure.ratings import rating_for

logger = logging.getLogger(__name__)

_ON_TYPES = set(OBLIGACIONES_NEGOCIABLES)
_FX_NOTE = "Pata pesos (…O) valuada MEP (ley AR) / CCL (ley EXT). …D=MEP, …C=CABLE."

# Ventana del badge de cambio de calificación (spec): 7 días desde el corte en que lo
# DETECTAMOS, no desde la fecha que declara FIX (esa puede ser previa al primer corte).
_CHG_WINDOW_DAYS = 7

_CACHE = {"key": None, "data": None}
_LOCK = threading.Lock()


def _pct100(v):
    return v * 100 if v is not None else None


def _ratings_as_of() -> str:
    """Corte vigente de las calificaciones (para el header del panel).

    Sale de `ratings.as_of()` —la fecha del último corte del store FIX— y no de la
    constante `AS_OF`: con el monitor diario el corte cambia solo, y una constante
    horneada mentiría sobre la vigencia del dato (`as_of()` ya cae a la semilla cuando
    todavía no hay ningún corte).
    """
    return _ratings.as_of()


def _norm_entidad(name) -> str:
    """Clave del join emisor↔entidad FIX: espacios colapsados y sin distinguir mayúsculas.

    El join se hace contra el emisor CANÓNICO que devuelve `rating_for` (el nombre del
    listado), así que en el caso normal coincide carácter a carácter con la entidad del
    store. La normalización sólo absorbe el ruido de tipeo del CSV semilla (doble espacio,
    capitalización distinta) — no es un segundo matcher: si el nombre no es el mismo, no
    hay badge, que es preferible a colgarle a un emisor el cambio de otro."""
    return " ".join(str(name or "").split()).casefold()


def _recent_rating_changes() -> dict:
    """`{entidad normalizada: cambio}` de la última semana, UNA consulta por dataset.

    Se resuelve acá (y no por fila) porque el panel tiene ~200 filas y el store es SQLite
    en disco. Si el store falla, el panel se sirve sin badges: un badge es un extra, no
    puede tumbar la tabla."""
    try:
        chg = _rhist.get_ratings_history_store().recent_changes(days=_CHG_WINDOW_DAYS)
    except Exception as e:                                    # noqa: BLE001
        logger.warning("on_service: sin cambios de calificación (%s)", e)
        return {}
    return {_norm_entidad(ent): c for ent, c in (chg or {}).items()}


def _rating_chg(chg) -> dict | None:
    """Vista de front del cambio: `{dir, from, to, fecha}` + las perspectivas.

    Contrato explícito en vez de pasar el dict del store: éste trae `area` (que el panel
    no usa) y podría crecer. Las perspectivas viajan porque el tooltip del caso `watch`
    (mismo rating, cambió la perspectiva) no tiene otra cosa que mostrar."""
    if not chg:
        return None
    return {"dir": chg.get("dir"), "from": chg.get("from"), "to": chg.get("to"),
            "fecha": chg.get("fecha"),
            "persp_from": chg.get("persp_from"), "persp_to": chg.get("persp_to")}




def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def _current_coupon(inst, today):
    """Cupón VIGENTE (TNA %) = interés del próximo flujo / fracción de año de su período,
    NORMALIZADO al nominal vigente (face 100). Para bonos regulares = la tasa fija; para
    STEP-UPS (ej. CLSIO) = la tasa del tramo actual. Fallback a coupon_rate (raw_fields)
    si no hay flujo futuro con interés.

    El cupón devenga sobre el nominal vigente (outstanding = suma de amortizaciones aún
    no pagadas a la fecha del próximo cupón), no sobre 100. Para ONs con capital factor
    < 1 (face residual < 100, ej. IRCFO vr=65) o ya amortizando, `nxt.interest/dcf` =
    coupon_rate × outstanding; dividir por `outstanding/100` recupera la TNA real. No-op
    para los bullets con face 100."""
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
    if not (dcf and dcf > 0):
        return inst.coupon_rate
    rate = nxt.interest / dcf
    outstanding = sum(cf.amortization for cf in inst.cashflows
                      if cf.date and cf.date >= nxt.date)
    if outstanding > 0:
        rate = rate / (outstanding / 100.0)
    return round(rate, 2)


def _build_on_dataset(state, fx=None) -> dict:
    today = date.today()
    chgs = _recent_rating_changes()
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
            price_usd = peso_leg_to_usd(inst, snap.price, fx)
        cy = FinancialEngine.current_yield(inst, price_usd, today) if price_usd else None
        convex = FinancialEngine.convexity(inst, m.tir, today) if m.tir is not None else None
        cal = rating_for(inst.short_name)   # calificación FIX SCR del emisor (o None)
        # Cambio reciente: se busca por el emisor CANÓNICO del match, no por short_name —
        # el catálogo dice "YPF SA" y FIX "YPF S.A.". Reusar el matcher (una sola regla de
        # identidad de emisor) evita un segundo criterio que se desincronice del primero.
        chg = chgs.get(_norm_entidad(cal["emisor"])) if cal else None
        bonds.append({
            "ticker": inst.ticker,
            "ccy": ccy,
            "ley": _ley_of(inst),
            "tipo": "HD" if inst.is_hard_dollar else "DL",  # Hard Dollar vs Dollar Linked
            "emisor": inst.short_name,
            "clase": inst.serie_clase,        # "Serie / Clase" del ABM (raw_fields.serie_clase)
            # ISIN del BONO (uno por fila del catalogo). Las patas MEP/CABLE comparten
            # el del titulo: el catalogo guarda un ISIN por instrumento, no por especie.
            "isin": inst.isin,
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
            "rating": cal["rating"] if cal else None,            # calificación largo plazo (FIX SCR)
            "rating_persp": cal["perspectiva"] if cal else None,  # perspectiva / rating watch
            "rating_grade": cal["grade"] if cal else None,        # categoría para colorear el badge
            # cambio de calificación detectado en los últimos 7 días (badge ▲/▼/⚑), o None
            "rating_chg": _rating_chg(chg),
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
            "ratings_as_of": _ratings_as_of(),   # corte vigente del listado FIX SCR
            "ratings_source": _CAL_SRC,
        },
    }


def get_on_dataset(state, fx=None, *, force: bool = False) -> dict:
    """Dataset `{generated, today, bonds, sectors, meta}` memoizado por (ciclo, hoy),
    donde `ciclo` es `state.last_refresh`: el sello del refresh loop, que avanza SIEMPRE.
    `fx` (FxProvider) se usa para pasar a USD la pata pesos en el Current Yield.

    Deliberadamente NO por `revision`. La revisión está gateada por la huella de los
    campos de mercado (`AppState._huella`), y este dataset depende ADEMAS del catálogo:
    `sector_override`, `short_name`, `clase`, `isin`, `ley`, `tipo`, `cupon`, `frec` y
    `emision` salen del `Instrument`, y una edición del ABM no mueve un solo campo de
    la huella. Con la revisión como clave, una edición no se veía hasta la próxima vez
    que se moviera un precio — fuera de rueda, horas — y `on.js` promete lo contrario.
    `last_refresh` restituye el "una vez por ciclo" que había antes del gating."""
    key = (state.last_refresh, str(date.today()))
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
