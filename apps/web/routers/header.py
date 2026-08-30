"""Header strip: todos los tipos de dólar (dolarapi) + reservas y riesgo país.

`GET /header/cards` → fragmento HTMX con las cards del header. Se incluye en
`base.html`, así que aparece en todas las páginas. Auto-refresh por polling
(`every 60s`): los valores cambian lento (FX varias veces/día, reservas a diario,
riesgo país 1-2x/día) y los providers ya cachean con TTL, así que cada poll que
no encuentra dato nuevo sólo re-renderiza desde cache.

Es un endpoint sync (`def`) → FastAPI lo corre en su threadpool, fuera del event
loop, igual que el resto de read-paths sync (FX/indices/argentinadatos vía _http).
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from apps.web.deps import get_bondterminal, get_fx, get_indices, get_state
from apps.web.templates import TEMPLATES as _TEMPLATES
from core.infrastructure.argentinadatos_provider import get_provider as get_argdatos

router = APIRouter()

# casa de dolarapi -> label corto para la card. Las casas no mapeadas se muestran
# igual (con su `nombre` crudo) al final, por si dolarapi agrega una nueva.
_DOLAR_LABELS = {
    "oficial": "Oficial",
    "mayorista": "Mayorista",
    "blue": "Blue",
    "bolsa": "MEP",
    "contadoconliqui": "CCL",
    "tarjeta": "Tarjeta",
    "cripto": "Cripto",
}
# Orden de presentación (oficiales primero, luego paralelos/financieros).
_DOLAR_ORDER = ["oficial", "mayorista", "blue", "bolsa", "contadoconliqui", "tarjeta", "cripto"]


def _dolar_cards(fx, prev_close: Optional[dict] = None) -> List[dict]:
    """[{label, compra, venta, var_pct}] para cada casa de dolarapi. Las del
    `_DOLAR_ORDER` primero; cualquier casa nueva no mapeada se agrega al final.

    `var_pct` = variación % de la venta live vs el cierre del día hábil anterior
    (`prev_close[casa]`, de argentinadatos). None si no hay cierre previo."""
    prev_close = prev_close or {}
    try:
        quotes = fx.get_all()
    except Exception:
        quotes = {}

    def _card(casa: str, q: dict) -> dict:
        venta = q.get("venta")
        prev = prev_close.get(casa)
        var_pct = (venta - prev) / prev * 100.0 if (venta is not None and prev) else None
        return {
            "label": _DOLAR_LABELS.get(casa) or q.get("nombre") or casa.title(),
            "compra": q.get("compra"),
            "venta": venta,
            "var_pct": var_pct,
        }

    cards: List[dict] = []
    seen = set()
    for casa in _DOLAR_ORDER:
        q = quotes.get(casa)
        if q and q.get("venta") is not None:
            cards.append(_card(casa, q))
            seen.add(casa)
    for casa, q in quotes.items():
        if casa not in seen and q and q.get("venta") is not None:
            cards.append(_card(casa, q))
    return cards


def _riesgo_pais(arg, bt=None) -> Optional[dict]:
    """Riesgo país para la card. Prefiere BondTerminal (refresca contra cotizaciones
    vivas, TTL 5min) y cae a ArgentinaDatos (corte 1-2×/día) si no hay dato.

    El número grande es el OFICIAL (Ambito/JP Morgan) en las DOS fuentes, para que la
    serie que ve el usuario sea la misma de siempre; el spread ponderado propio de
    BondTerminal —calculado sobre los bonos del EMBI AR— va al tooltip como extra."""
    try:
        d = bt.get_riesgo_pais() if bt is not None else None
    except Exception:
        d = None
    if d and d.get("ambito_bps") is not None:
        return {
            "valor": d["ambito_bps"],
            "fecha": d.get("fecha"),
            "delta_abs": d.get("ambito_delta_1d"),
            "delta_pct": None,
            "spread_bps": d.get("valor_bps"),
            "fuente": "BondTerminal",
        }
    try:
        return arg.get_riesgo_pais()
    except Exception:
        return None


def _dolares_prev_close(arg) -> dict:
    try:
        return arg.get_dolares_prev_close()
    except Exception:
        return {}


@router.get("/header/cards", response_class=HTMLResponse)
def header_cards(request: Request, fx=Depends(get_fx), indices=Depends(get_indices),
                 bt=Depends(get_bondterminal)):
    arg = get_argdatos()  # singleton: riesgo país (fallback) + cierre previo de dólares
    ctx = {
        "dolares": _dolar_cards(fx, _dolares_prev_close(arg)),
        "reservas": indices.get_reservas_brutas(),
        "reservas_delta": indices.get_reservas_delta(),
        "riesgo_pais": _riesgo_pais(arg, bt),
    }
    return _TEMPLATES.TemplateResponse(request, "fragments/header_cards.html", ctx)


@router.get("/health/badge", response_class=HTMLResponse)
def health_badge(request: Request, state=Depends(get_state)):
    """Indicador de frescura para el header (O1): verde = al día, ámbar = datos
    viejos, rojo = último refresh con error. Se poll-ea cada 15s desde base.html →
    presente en todas las páginas. Umbral centralizado en AppState.status()."""
    st = state.status()
    return _TEMPLATES.TemplateResponse(request, "fragments/header_status.html", {"st": st})
