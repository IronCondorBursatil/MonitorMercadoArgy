"""Router de paneles de bonos (HTMX SSR).

`GET /` → página index con los 6 paneles de bonos.
`GET /panels/{id}/rows` → fragmento <tbody> que HTMX refresca cada 5s.

Reemplaza el polling global de `/api/snapshot` + el render JS de `app.js` por
fragmentos server-side. Reusa los InstrumentMetrics ya calculados en AppState
(motor Fase 1) y el column-schema del http.server original.

Row-building logic lives in `apps/web/panels_rows.py`; this module only wires
FastAPI routes, dependency injection, and the CI-metrics memoization cache.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from apps.web.json_script import json_for_script
from apps.web.deps import get_fx, get_indices, get_provider, get_repo, get_rofex, get_state
from apps.web.templates import TEMPLATES as _TEMPLATES
from apps.web.panels_rows import (  # noqa: F401 — re-exported for tests + external callers
    _ley_of, _ticker_ccy, _fit_log_curve, _spread_carry, _rv_map,
    _next_coupon_date, _pct, _row_values, _fmt, _cell_class,
    _build_rv_rows, _build_panel_lider_rows, _build_futuros_rows,
    _implied_rates, _peso_tea_curve, _futuros_label, _build_futuros_share,
    _build_bei_rows, _build_rows, _chart_payload, panel_columns,
    _share_full_cols, _drop_empty_share_cols,
    _ALL_BOND_COLS, _SHARE_DROP_COLS,
    _RV_GROUPS, _ONE_MONTH_YEARS, _TASA_FIJA_TYPES,
)
from config.settings import settings
from core.holiday_engine import settlement_byma_date
from core.infrastructure.provider_hub import HubMarketDataProvider
from core.use_cases.generate_report import GenerateMonitorReport

# Schema declarativo (columnas + registro PANELS + filtros) -> panels_schema.py.
from apps.web.routers.panels_schema import (  # noqa: E402
    PANELS, PANEL_ORDER, CCY_FILTER_PANELS, SETTLE_FILTER_PANELS,
    LEY_FILTER_PANELS, _HL_COL_KEY,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Layout por defecto del dashboard (posiciones + paneles cerrados + columnas).
# Se guarda junto a la .db (fuera del working tree); si no existe, el front usa el
# auto-layout. El usuario lo setea con "Guardar como default" en el menú CONFIG.
_LAYOUT_FILE = str(Path(str(settings.catalog_db)).parent / "dashboard_layout.json")


def _read_default_layout() -> str:
    """JSON del layout default escapado para embeber en <script>, o 'null'. Se
    re-serializa (no se devuelve el archivo crudo) para neutralizar un payload
    malicioso que un POST a /panels/layout pudiera haber guardado."""
    try:
        with open(_LAYOUT_FILE, "r", encoding="utf-8") as f:
            obj = json.loads(f.read())
        return json_for_script(obj)
    except (OSError, ValueError):
        return "null"


# ── CI metrics: memoización por (revision, panel_id) ────────────────────────

_CI_METRICS_CACHE: dict = {}
# `panel_rows` es un handler SYNC → FastAPI lo corre en el threadpool de anyio, y los
# dos paneles con selector CI disparan su hx-get con el MISMO evento sse:refresh: hay
# dos hilos dentro de `_ci_metrics` en cada ciclo. Sin este lock la lectura y la purga
# eran read-modify-write no atómicos (doble `del` → KeyError, iteración mientras el
# otro borra → RuntimeError) y el fragmento salía 500. Mismo patrón que fci_service.
_CI_METRICS_LOCK = threading.Lock()


def _ci_metrics(panel_id: str, request: Request, hist_provider,
                ciclo=None) -> Optional[list]:
    """Métricas on-demand del plazo CI (T+0): corre el motor para los tipos del panel
    leyendo el snapshot CI del hub. Todo en memoria (el hub ya tiene CI del refresh,
    sin red extra). None si el panel no tiene tipos (no aplica).

    Memoizado por (ciclo, panel_id), donde `ciclo` es `state.last_refresh` — el sello
    del ciclo del refresh loop, que avanza SIEMPRE.

    Deliberadamente NO por `revision`: la revisión está gateada por la huella del
    snapshot de **24hs** (`AppState._huella`), y esto corre el motor sobre el snapshot
    **CI**, que el hub guarda por separado. O sea que la clave no rastreaba ninguno de
    los inputs de lo que cachea. Antes del gating no se notaba porque la revisión subía
    cada 5s y daba vuelta el memo sola; con el gating se congela y el panel CI podía
    servir precios viejos. `last_refresh` restituye exactamente ese "una vez por ciclo"."""
    types = PANELS[panel_id][1]
    if not types:
        return None

    cache_key = (ciclo, panel_id)
    with _CI_METRICS_LOCK:               # get() en vez de `in` + []: una sola lectura
        hit = _CI_METRICS_CACHE.get(cache_key)
    if hit is not None:                  # (una lista vacía también es un hit válido)
        return hit

    hub = request.app.state.hub
    ci_provider = HubMarketDataProvider(hub, hist_provider, settle="CI")
    # CI (T+0): settle_date=hoy descuenta TIR/MD desde hoy; settle_lag=0 mueve el ref
    # CER de la V.Téc a T+0 también → todo el cálculo en consonancia con el precio CI.
    app_state = request.app.state
    result = GenerateMonitorReport(
        get_repo(), ci_provider,
        indices=getattr(app_state, "indices", None),
        fx=getattr(app_state, "fx", None),
    ).execute(list(types), settle_date=date.today(), settle_lag=0)
    with _CI_METRICS_LOCK:
        # Purgar las entradas de ciclos anteriores (el sello es monótono creciente).
        for k in [k for k in _CI_METRICS_CACHE if k[0] != ciclo]:
            _CI_METRICS_CACHE.pop(k, None)
        _CI_METRICS_CACHE[cache_key] = result
    return result


# ── FastAPI route handlers ───────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def index(request: Request, state=Depends(get_state)):
    panels = [{"id": pid, "title": PANELS[pid][0], "columns": panel_columns(pid),
               "ccy_filter": pid in CCY_FILTER_PANELS,
               "ley_filter": pid in LEY_FILTER_PANELS,
               "settle_filter": pid in SETTLE_FILTER_PANELS,
               "hl_key": _HL_COL_KEY.get(pid),
               "chartable": bool(PANELS[pid][1]),
               "rows": []} for pid in PANEL_ORDER]
    return _TEMPLATES.TemplateResponse(
        request, "pages/index.html",
        {"panels": panels, "last_refresh": state.last_refresh,
         "default_layout": _read_default_layout()},
    )


@router.post("/panels/layout")
async def save_default_layout(request: Request):
    """Guarda el layout actual ({layout, hidden, cols}) como default del dashboard."""
    raw = await request.body()
    try:
        obj = json.loads(raw)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    def _write():
        os.makedirs(os.path.dirname(_LAYOUT_FILE), exist_ok=True)
        with open(_LAYOUT_FILE, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    try:
        await asyncio.to_thread(_write)   # I/O de disco fuera del event loop
    except OSError as e:
        logger.warning("No se pudo guardar el layout default: %s", e)
        return JSONResponse({"ok": False}, status_code=500)
    logger.info("Dashboard: layout default guardado.")
    return JSONResponse({"ok": True})


@router.delete("/panels/layout")
def clear_default_layout():
    """Borra el layout default (vuelve al auto-layout)."""
    try:
        os.remove(_LAYOUT_FILE)
    except OSError:
        pass
    return JSONResponse({"ok": True})


@router.get("/panels/{panel_id}/rows", response_class=HTMLResponse)
def panel_rows(panel_id: str, request: Request, settle: str = "24", state=Depends(get_state),
               provider=Depends(get_provider), rofex=Depends(get_rofex),
               fx=Depends(get_fx), indices=Depends(get_indices)):
    cols = panel_columns(panel_id)
    if panel_id == "futuros":
        rows = _build_futuros_rows(rofex, fx, indices)
    elif settle.upper() == "CI" and panel_id in SETTLE_FILTER_PANELS:
        metrics = _ci_metrics(panel_id, request, provider, ciclo=state.last_refresh)
        rows = _build_rows(panel_id, state, provider, metrics_override=metrics or [])
    else:
        rows = _build_rows(panel_id, state, provider)
    return _TEMPLATES.TemplateResponse(
        request, "fragments/panel_rows.html",
        {"rows": rows, "ncols": len(cols)},
    )


@router.get("/panels/{panel_id}/chart", response_class=HTMLResponse)
def panel_chart(panel_id: str, request: Request, ccy: str = "", ley: str = "",
                state=Depends(get_state)):
    ccy_set = {c.strip().upper() for c in ccy.split(",") if c.strip()} or None
    ley_set = {s.strip().upper() for s in ley.split(",") if s.strip()} or None
    datasets = _chart_payload(panel_id, state, ccy_set, ley_set)
    title = PANELS.get(panel_id, (panel_id,))[0]
    y_label = "TNA" if panel_id == "tasa_fija" else "TIR"
    return _TEMPLATES.TemplateResponse(
        request, "fragments/panel_chart.html",
        {"title": title, "datasets_json": json_for_script(datasets), "y_label": y_label},
    )


# Bajada (subtítulo) por panel para la foto — da contexto al cliente.
_PANEL_DESC = {
    "bonares": "Soberanos en dólares · ley local y NY",
    "bopreales": "Bopreales (BCRA) · hard-dollar",
    "cer": "Pesos ajustados por inflación (CER)",
    "tasa_fija": "Pesos a tasa fija",
    "tamar": "Tasa variable TAMAR / Dual",
    "dolar_linked": "Atados al dólar oficial",
    "obligaciones_negociables": "Obligaciones Negociables · deuda corporativa USD",
}

# Reparto del alto gráfico:tabla en la foto, por panel.
_SHARE_CHART_MULT = {"tasa_fija": 1.0}
_SHARE_CHART_MULT_DEFAULT = 0.5


@router.get("/panels/{panel_id}/share", response_class=HTMLResponse)
def panel_share(panel_id: str, request: Request, ccy: str = "", ley: str = "",
                state=Depends(get_state),
                provider=Depends(get_provider), rofex=Depends(get_rofex),
                fx=Depends(get_fx), indices=Depends(get_indices)):
    """Popup 'compartir' para WhatsApp: tabla + curva TIR×MD arriba (solo paneles
    chartables), en la MONEDA elegida en el panel (`ccy`, ej. MEP)."""
    if panel_id == "futuros":
        data = _build_futuros_share(rofex, fx, indices, state)
        resp = _TEMPLATES.TemplateResponse(
            request, "fragments/futuros_share.html",
            {"contracts_json": json_for_script(data.get("contracts", [])),
             "spot_txt": (f"{data['spot']:,.2f}" if data.get("spot") else "—"),
             "has_peso_curve": data.get("has_peso_curve", False)},
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    today = date.today()
    try:
        settle = settlement_byma_date(today, 1).strftime("%d/%m/%Y")
    except Exception:
        settle = ""
    title, _types, full_cols = PANELS.get(panel_id, (panel_id, set(), []))
    if _types:
        cols = _share_full_cols(full_cols, panel_id)
        rows = _build_rows(panel_id, state, provider, cols_override=cols)
    else:
        cols = list(full_cols)
        rows = _build_rows(panel_id, state, provider)
    ccy_set = {c.strip().upper() for c in ccy.split(",") if c.strip()} or None
    if panel_id in CCY_FILTER_PANELS:
        if ccy_set is None:
            ccy_set = {"MEP"}
        ccy_label = " + ".join(sorted(ccy_set))
    else:
        ccy_set, ccy_label = None, ""
    if ccy_set is not None:
        rows = [r for r in rows if r.get("ccy") is None or r["ccy"] in ccy_set]
    ley_set = {s.strip().upper() for s in ley.split(",") if s.strip()} or None
    if panel_id not in LEY_FILTER_PANELS:
        ley_set = None
    if ley_set is not None:
        rows = [r for r in rows if r.get("ley") is None or r["ley"] in ley_set]
        ccy_label = (ccy_label + " · " if ccy_label else "") + \
            "Ley " + "+".join(sorted(ley_set))
    cols, rows = _drop_empty_share_cols(cols, rows)
    datasets = _chart_payload(panel_id, state, ccy_set, ley_set)
    y_label = "TNA" if panel_id == "tasa_fija" else "TIR"
    resp = _TEMPLATES.TemplateResponse(
        request, "fragments/panel_share.html",
        {"title": title, "columns": cols, "rows": rows,
         "hl_key": _HL_COL_KEY.get(panel_id),
         "chart_mult": _SHARE_CHART_MULT.get(panel_id, _SHARE_CHART_MULT_DEFAULT),
         "desc": _PANEL_DESC.get(panel_id, ""), "ccy_label": ccy_label,
         "asof": today.strftime("%d/%m/%Y"), "settle": settle,
         "badge": ("%s vs Duración" % y_label),
         "datasets_json": json_for_script(datasets), "has_chart": bool(datasets),
         "y_label": y_label},
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp
