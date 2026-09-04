"""ABM de instrumentos (HTMX) — backend SQLite transaccional (§5.5).

GET    /abm                     → lista + selector de hoja + form.
GET    /abm/form                → form schema-driven de una hoja (prefill si ticker).
POST   /abm/preview_cashflows   → schedule PROPUESTO desde los campos del form (no persiste).
POST   /abm/save                → alta/edición (SQLAlchemy txn) + refresh del cache.
DELETE /abm/instrument/{ticker} → baja + refresh.

Fase 9 — el POST de `/abm/save` lleva el SCHEDULE (`cf_date`/`cf_amort`/`cf_interest`,
la tabla editable del cajón). Antes llamaba `save_instrument(sheet, fields)` sin flujos y
el store sintetizaba al guardar; como `cashflow_synth` lee el reloj para el step-up del
cupón, el schedule que quedaba en la DB dependía del día del alta. Ahora la síntesis vive
sólo acá, en `/abm/preview_cashflows`: el operador la ve, la corrige y la manda de vuelta.

Escribe el catálogo SQLite vía `apps.web.instruments_abm` (transaccional) y
refresca el cache en memoria del `CatalogRepository` desde SQLite (sin re-leer
el Excel: el master ya es solo semilla).
"""

from __future__ import annotations

import asyncio
import html
import logging

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from apps.web import instruments_abm as abm_store
from apps.web.bond_detail import calculate
from apps.web.deps import (
    get_fx, get_hub, get_indices, get_provider, get_repo, get_state,
)
from apps.web.templates import TEMPLATES as _TEMPLATES
from core.infrastructure.byma.universe import (
    categories, count, count_unloaded, prefill_for, search_byma_grouped,
)

logger = logging.getLogger(__name__)
router = APIRouter()


_DEFAULT_SHEET = "Obligaciones_Negociables"


def _price_of(state):
    return state.price_of if state is not None else None


def _render_list(request: Request, sheet: str, state, error: str = "") -> HTMLResponse:
    """Tabla de completitud (adaptativa por hoja) para `sheet` ('' = todas).

    `oob=True`: la respuesta arrastra además un swap out-of-band que espeja `error` en
    `#abm-drawer-err`, dentro del cajón. Sin eso, un rechazo del save iniciado desde el
    tab «Universo BYMA» aterriza en `#abm-list`, que ahí está con `display:none`, y el
    operador no ve NADA. Va vacío en el camino feliz para limpiar el error anterior."""
    cov = abm_store.list_instruments_coverage(_price_of(state), sheet or None)
    return _TEMPLATES.TemplateResponse(request, "fragments/abm_list.html", {
        "instruments": cov, "cols": abm_store.coverage_columns(sheet),
        "sheet": sheet, "error": error, "oob": True,
    })


@router.get("/abm", response_class=HTMLResponse)
def abm_page(request: Request, state=Depends(get_state)):
    cov = abm_store.list_instruments_coverage(_price_of(state), _DEFAULT_SHEET)
    try:
        unloaded = count_unloaded()
    except Exception:  # noqa: BLE001 — best-effort; el contador no debe romper la página
        unloaded = None
    # Títulos cargados por hoja → badge (N) en cada chip de categoría.
    loaded = abm_store.list_instruments()
    sheet_counts: dict = {}
    for it in loaded:
        sheet_counts[it["sheet"]] = sheet_counts.get(it["sheet"], 0) + 1
    return _TEMPLATES.TemplateResponse(request, "pages/abm.html", {
        "instruments": cov,
        "cols": abm_store.coverage_columns(_DEFAULT_SHEET),
        "sheet": _DEFAULT_SHEET,
        "sheets": list(abm_store.SHEET_SCHEMAS.keys()),
        "loaded_total": len(loaded),
        "sheet_counts": sheet_counts,
        "unloaded": unloaded,
        "byma_cats": categories(),
        "byma_count": count(),
    })


@router.get("/abm/list", response_class=HTMLResponse)
def abm_list(request: Request, sheet: str = "", state=Depends(get_state)):
    """Tabla de completitud de una hoja ('' = todas) — la cambian los chips de hoja."""
    return _render_list(request, sheet, state)


_UNIVERSE_PAGE = 200  # tamaño del lote del scroll infinito


def _fmt_monto(v: Optional[float]) -> str:
    """Monto operado → string compacto (1.2M / 380k / 250). '—' si nulo/cero."""
    if not v or v <= 0:
        return "—"
    if v >= 1e6:
        return f"{v / 1e6:.1f}M"
    if v >= 1e3:
        return f"{v / 1e3:.0f}k"
    return f"{v:.0f}"


def _attach_volumes(rows, hub) -> None:
    """Adjunta a cada grupo, desde el snapshot vivo del hub:
    - el **monto operado del día** por moneda (`mh_<ccy>` crudo + `mh_<ccy>_f` M/k).
    - `px`: {ticker: {px, src}} para colorear cada ticker según tenga precio (referencia).
      `src` = LAST si operó hoy (volumen/operaciones), si no CLOSE (cierre previo del feed).
    Best-effort: si el hub no está listo, queda todo vacío y la tabla muestra '—'."""
    try:
        snap = hub.snapshot() if hub else {}
    except Exception:  # noqa: BLE001 — el hub puede no estar listo
        snap = {}

    def _today(cell: str):
        s = sum((getattr(snap.get(t.upper()), "v", None) or 0.0)
                for t in (cell or "").split(" · ") if t)
        return s or None

    for g in rows:
        for key, slot in (("ars", "pesos"), ("mep", "mep"), ("cable", "cable")):
            v = _today(g["primary"][slot])
            g[f"mh_{key}"], g[f"mh_{key}_f"] = v, _fmt_monto(v)
        px: dict = {}
        for bucket in ("primary", "especial"):
            for slot in ("pesos", "mep", "cable"):
                for t in (g.get(bucket, {}).get(slot) or "").split(" · "):
                    t = t.strip()
                    if not t:
                        continue
                    row = snap.get(t.upper())
                    c = getattr(row, "c", None) if row else None
                    if c:
                        traded = bool(getattr(row, "v", None) or getattr(row, "q_op", None))
                        px[t] = {"px": c, "src": "LAST" if traded else "CLOSE"}
        g["px"] = px


@router.get("/abm/universe", response_class=HTMLResponse)
def abm_universe(request: Request, q: str = "", cat: str = "", page: int = 0,
                 hub=Depends(get_hub), state=Depends(get_state)):
    """Buscador del universo BYMA (ticker/ISIN/emisor + filtro de categoría),
    1 fila por título valor (moneda → columna), con **scroll infinito**.

    `page==0` (carga inicial / cambio de filtro) → shell completo (meta + thead +
    1er lote). `page>0` (centinela revelado al scrollear) → solo el lote de filas +
    su próximo centinela, que reemplaza al anterior vía outerHTML."""
    page = max(0, page)
    # Con una categoría elegida (caso de uso real) traemos TODO el set de una (cientos
    # de títulos) → el filtro por columna del cliente opera sobre el universo completo,
    # sin huecos del scroll infinito. Sin categoría (≈miles), seguimos paginando.
    if cat.strip():
        rows, total = search_byma_grouped(q, cat, limit=100_000, offset=0)
        has_next = False
        page = 0
    else:
        rows, total = search_byma_grouped(q, cat, limit=_UNIVERSE_PAGE, offset=page * _UNIVERSE_PAGE)
        has_next = (page * _UNIVERSE_PAGE + len(rows)) < total
    _attach_volumes(rows, hub)  # monto operado ARS/MEP/CABLE (día + promedio)
    # Legislación (ON): columna extra Ticker↔ISIN, solo al filtrar Obligaciones Negociables.
    show_leg = cat.strip() == "Obligaciones Negociables"
    ctx = {"rows": rows, "q": q, "cat": cat, "page": page, "has_next": has_next,
           "show_leg": show_leg}
    if page == 0:
        ctx["total"] = total
        # Hora del snapshot mostrado (el monto operado se refresca solo a pedido,
        # botón ↻): usar last_refresh del AppState — la edad REAL del dato, el mismo
        # reloj que el 'act' del header. El wall-clock del render mentiría si el
        # refresh loop está caído (el dato sería viejo y la hora "fresca").
        lr = state.last_refresh
        ctx["asof"] = lr.strftime("%H:%M:%S") if lr else None
        return _TEMPLATES.TemplateResponse(request, "fragments/abm_universe.html", ctx)
    return _TEMPLATES.TemplateResponse(request, "fragments/abm_universe_rows.html", ctx)


def _live_metrics(state, values: dict, key: str):
    """Métricas vivas (TIR/MD/V.Téc/paridad/precio) del bono.
    Prioridad: MEP (D) → CABLE (C) → ARS (O) → key como fallback."""
    candidates = [
        values.get("ticker_mep"),
        values.get("ticker_ccl"),
        values.get("ticker_ars"),
        key,
    ]
    for t in candidates:
        if not t:
            continue
        m = state.by_ticker(t)
        if m is not None:
            return {
                "ticker": t,
                "price": m.snapshot.price if m.snapshot else None,
                "tir": m.tir, "md": m.duration,
                "vtec": m.technical_value, "par": m.parity,
            }
    return None


@router.get("/abm/form", response_class=HTMLResponse)
def abm_form(request: Request, sheet: str = "", key: str = "", prefill: str = "",
             state=Depends(get_state)):
    """Form del editor (cuerpo del cajón). `key` → edición prefilleada desde la DB.
    `prefill` (key de un grupo del Universo) → alta de 1 clic: deduce la hoja de la
    categoría BYMA y precarga tickers/ISIN/emisor/ley."""
    values, cashflows, metrics = {}, [], None
    if prefill:
        pf = prefill_for(prefill)
        if pf:
            sheet, values = pf["sheet"], pf["fields"]
    if sheet not in abm_store.SHEET_SCHEMAS:
        return HTMLResponse("<div class='err'>Hoja desconocida</div>", status_code=400)
    if key and not prefill:
        # `key` es cualquier ticker del bono → prefill con sus 3 slots de moneda.
        inst = abm_store.get_instrument(key)
        if inst:
            values = inst.get("fields", {})
            cashflows = inst.get("cashflows", [])
        metrics = _live_metrics(state, values, key)
    return _TEMPLATES.TemplateResponse(request, "fragments/abm_form.html", {
        "sheet": sheet,
        "fields": abm_store.SHEET_SCHEMAS[sheet]["fields"],
        "label": abm_store.SHEET_SCHEMAS[sheet]["label"],
        "values": values,
        "ticker": key,
        "cashflows": cashflows,
        "metrics": metrics,
    })


@router.post("/abm/calc", response_class=HTMLResponse)
def abm_calc(request: Request, ticker: str = Form(...),
             price: Optional[float] = Form(None), tir_pct: Optional[float] = Form(None),
             repo=Depends(get_repo), provider=Depends(get_provider),
             indices=Depends(get_indices), fx=Depends(get_fx)):
    """Recalcula precio↔TIR de un ticker concreto del bono (mismo motor que el modal
    de detalle). El selector de moneda elige la pata: USD (D/C) o pesos."""
    if tir_pct is not None and price is None:
        res = calculate(ticker, repo, provider, indices, fx, mode="from_tir",
                        tir=tir_pct / 100.0, settlement_lag=1)
    else:
        res = calculate(ticker, repo, provider, indices, fx, mode="from_price",
                        price=price, price_mode="dirty", settlement_lag=1)
    return _TEMPLATES.TemplateResponse(request, "fragments/calc_result.html", {"res": res})


# Claves de la tabla de flujos del cajón: viajan en el MISMO POST que el resto del form
# (la tabla vive dentro del <form> de /abm/save) pero NO son campos del instrumento.
_CF_FORM_KEYS = ("cf_ticker", "cf_date", "cf_amort", "cf_interest")


def _form_fields(form) -> dict:
    """Campos del instrumento: todo el form menos `sheet` y la tabla de flujos."""
    return {k: v for k, v in form.items()
            if k != "sheet" and k not in _CF_FORM_KEYS}


def _form_cashflows(form):
    """Schedule que manda el form, o **None** si no vino ninguna fila.

    None ≠ [] a propósito: None significa "el form no trae schedule" y deja que
    `save_instrument` conserve el que el bono ya tenía (edición que sólo toca los
    términos). Una tabla presente pero con todas las filas en blanco también da None —
    vaciar el flujo de fondos es una operación destructiva y tiene su propio botón
    (`POST /abm/cashflows`), no puede ser el efecto colateral de un submit."""
    dates = form.getlist("cf_date")
    if not dates:
        return None
    amorts, ints = form.getlist("cf_amort"), form.getlist("cf_interest")
    rows = [{"date": d, "amortization": a or 0, "interest": i or 0}
            for d, a, i in zip(dates, amorts, ints) if (d or "").strip()]
    return rows or None


@router.post("/abm/preview_cashflows", response_class=HTMLResponse)
async def abm_preview_cashflows(request: Request):
    """Schedule PROPUESTO desde los campos del form (síntesis pura, no persiste nada).
    Devuelve las filas de la tabla editable; el operador las revisa y el submit de
    `/abm/save` las manda. Es el ÚNICO consumidor del synth desde la ABM: así el reloj
    que lee `cashflow_synth` no puede contaminar lo que queda en la DB."""
    form = await request.form()
    sheet = str(form.get("sheet") or "")
    # to_thread: la síntesis de un bullet a 30 años son cientos de relativedelta.
    # El `sheet` va aparte de los campos porque `preview_cashflows` lo necesita para
    # resolver el tipo y aplicar la misma regla por tipo que `/abm/save`.
    prev = await asyncio.to_thread(abm_store.preview_cashflows, _form_fields(form), sheet)
    return _TEMPLATES.TemplateResponse(request, "fragments/abm_cf_rows.html", prev)


@router.post("/abm/save", response_class=HTMLResponse)
async def abm_save(request: Request, sheet: str = Form(...),
                   repo=Depends(get_repo), state=Depends(get_state)):
    form = await request.form()
    fields = _form_fields(form)
    cashflows = _form_cashflows(form)
    try:
        # to_thread: SQLite + repo.reload() (relee ~550 instrumentos con sus cashflows)
        # son ~130-200 ms de I/O+CPU sincrónico. En la corrutina frenaban el event loop
        # y con él todos los SSE. CLAUDE.md ya decía que esto corría en to_thread.
        def _save():
            abm_store.save_instrument(sheet, fields, cashflows)
            repo.reload()                              # refresca el cache desde SQLite
        await asyncio.to_thread(_save)
    except (ValueError, KeyError) as e:
        # NUNCA tragar el error: el operador tiene que saber que NO se guardó
        # (antes esto era `pass` y el alta "desaparecía" sin aviso).
        logger.warning("ABM save falló (%s): %s", sheet, e)
        return _render_list(request, sheet, state, error=str(e))
    return _render_list(request, sheet, state)


@router.post("/abm/cashflows", response_class=HTMLResponse)
async def abm_cashflows(request: Request, repo=Depends(get_repo)):
    """Guarda el flujo de fondos EDITADO en el cajón (filas date/amort/interest) →
    `save_cashflows` (delete+insert) + reload en caliente. Devuelve un mini-flash."""
    form = await request.form()
    ticker = (form.get("cf_ticker") or "").strip()
    dates = form.getlist("cf_date")
    amorts = form.getlist("cf_amort")
    ints = form.getlist("cf_interest")
    cfs = [{"date": d, "amortization": a or 0, "interest": i or 0}
           for d, a, i in zip(dates, amorts, ints) if (d or "").strip()]
    try:
        def _save():                      # to_thread: ver nota en abm_save
            abm_store.save_cashflows(ticker, cfs)
            repo.reload()
        await asyncio.to_thread(_save)
    except (ValueError, KeyError) as e:
        # `e` arrastra texto que vino del FORM (ticker/fechas/importes): este flash es
        # HTML armado a mano —el resto de la ABM va por Jinja, que autoescapa— así que
        # sin escapar es XSS reflejado (mismo defecto que el 404 de /bond/{ticker}).
        return HTMLResponse(
            f'<span class="abm-flash" style="color:var(--neg)">⚠ {html.escape(str(e))}</span>')
    return HTMLResponse(f'<span class="abm-flash">✓ {len(cfs)} flujos guardados</span>')


@router.delete("/abm/instrument/{ticker}", response_class=HTMLResponse)
def abm_delete(ticker: str, request: Request, repo=Depends(get_repo), state=Depends(get_state)):
    res = abm_store.delete_instrument(ticker)
    repo.reload()
    return _render_list(request, res.get("sheet") or "", state)
