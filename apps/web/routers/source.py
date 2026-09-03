"""Selector de fuente de datos live (BYMA open / BYMA realtime / Data912).

`GET /source/status` → estado + modos disponibles (JSON).
`GET /source/menu`   → fragmento HTMX del control del header.
`POST /source/select`→ cambia la fuente activa en runtime (hot-swap del hub).

El cambio es **global** (no per-cliente): el hub mantiene una sola fuente activa.
El próximo ciclo del refresh loop (≤ refresh_sec) recomputa las métricas con la
fuente nueva; el `_notify()` empuja el SSE para que los paneles se refresquen.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from apps.web.deps_auth import get_admin_user_html
from apps.web.templates import TEMPLATES as _TEMPLATES
from core.infrastructure.byma.credentials import clear_credentials, save_credentials
from core.infrastructure.byma.sources import (
    MODES, BymaRealtimeError, BymaRealtimeSource, make_source, source_label,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _mode_list() -> list[dict]:
    out = []
    for m in MODES:
        available = True
        note = ""
        if m == BymaRealtimeSource.mode and not BymaRealtimeSource.has_credentials():
            available = False
            note = "Configurá BYMADATA_USER / BYMADATA_PASS en .env"
        out.append({"mode": m, "label": source_label(m), "available": available, "note": note})
    return out


def _status(request: Request) -> dict:
    hub = request.app.state.hub
    return {
        "active": hub.active_mode,
        "label": hub.active_label,
        "delayed": hub.is_delayed,
        "realtime_ready": BymaRealtimeSource.has_credentials(),
        "modes": _mode_list(),
    }


@router.get("/source/status")
def source_status(request: Request):
    return JSONResponse(_status(request))


@router.get("/source/menu", response_class=HTMLResponse)
def source_menu(request: Request):
    return _TEMPLATES.TemplateResponse(request, "fragments/source_menu.html", _status(request))


@router.post("/source/select", response_class=HTMLResponse)
async def source_select(request: Request, mode: str = Form(...),
                        _admin=Depends(get_admin_user_html)):
    hub = request.app.state.hub
    app_state = request.app.state.app_state
    mode = (mode or "").strip()
    if mode not in MODES:
        return _TEMPLATES.TemplateResponse(
            request, "fragments/source_menu.html",
            {**_status(request), "error": f"Modo desconocido: {mode}"}, status_code=400)
    try:
        src = make_source(mode)
    except (BymaRealtimeError, ValueError) as e:
        return _TEMPLATES.TemplateResponse(
            request, "fragments/source_menu.html",
            {**_status(request), "error": str(e)}, status_code=400)

    hub.set_source(src)
    app_state.set_data_source(hub.active_mode, hub.active_label, hub.is_delayed)
    # Despierta el SSE: los paneles re-piden filas; el refresh loop recomputa con la
    # fuente nueva en el próximo ciclo (≤ refresh_sec).
    await app_state._notify()
    logger.info("Fuente de datos cambiada a %s (%s).", hub.active_mode, hub.active_label)
    return _TEMPLATES.TemplateResponse(request, "fragments/source_menu.html", _status(request))


@router.post("/source/credentials", response_class=HTMLResponse)
async def source_credentials(request: Request, user: str = Form(...), password: str = Form(...),
                             _admin=Depends(get_admin_user_html)):
    """Valida la clave BYMA realtime (login OAuth), la guarda en `.env` y activa el
    modo tiempo real. Si el login falla, NO guarda (devuelve 400 con el motivo)."""
    hub = request.app.state.hub
    app_state = request.app.state.app_state
    user, password = (user or "").strip(), (password or "").strip()

    def _err(msg: str):
        return _TEMPLATES.TemplateResponse(
            request, "fragments/source_menu.html",
            {**_status(request), "error": msg}, status_code=400)

    if not user or not password:
        return _err("Usuario y contraseña son requeridos.")

    # Validar el login ANTES de persistir (el host OAuth www.bymadata.com.ar NO está
    # geo-bloqueado → andan creds erróneas en feedback inmediato, sin guardar basura).
    probe = BymaRealtimeSource(username=user, password=password)
    try:
        await probe._ensure_token()
    except Exception as e:  # noqa: BLE001 — auth/red → feedback al usuario, no 500
        return _err(f"No se pudo validar la clave: {str(e)[:160]}")

    # `save_credentials` valida su propio contrato (no-vacíos, sin separadores de línea
    # ni `=` en el usuario) y PROPAGA ValueError. Sin este try la excepción salía por
    # arriba y, como `apps/web/app.py` no registra `exception_handler(ValueError)`,
    # terminaba en un **500** con traza: un usuario BYMA con `=` en el nombre pasa el
    # probe OAuth (login válido) y recién moría acá. Es entrada del usuario → 400 con el
    # motivo, como el resto del handler. (Contrato ligado por
    # tests/test_rem_R2_infra_credentials_contrato.py, que compara el AST de este
    # router contra la marca `STATUS-HTTP-REAL` de `save_credentials`.)
    try:
        save_credentials(user, password)        # .env + os.environ
    except ValueError as e:
        return _err(str(e))
    except OSError as e:                        # .env no escribible (permisos, disco)
        logger.warning("No se pudo persistir el .env de BYMA: %s", e)
        return _err(f"No se pudo guardar la clave en .env: {e}")
    hub.set_source(probe)                        # ya logueado (reusa el token validado)
    app_state.set_data_source(hub.active_mode, hub.active_label, hub.is_delayed)
    await app_state._notify()
    logger.info("BYMA realtime validado y activado tras guardar credenciales.")
    return _TEMPLATES.TemplateResponse(request, "fragments/source_menu.html", _status(request))


@router.post("/source/credentials/clear", response_class=HTMLResponse)
async def source_credentials_clear(request: Request, _admin=Depends(get_admin_user_html)):
    """Borra la clave guardada; si la fuente activa era realtime, vuelve a open."""
    hub = request.app.state.hub
    app_state = request.app.state.app_state
    clear_credentials()
    if hub.active_mode == BymaRealtimeSource.mode:
        hub.set_source(make_source("byma_open"))
        app_state.set_data_source(hub.active_mode, hub.active_label, hub.is_delayed)
        await app_state._notify()
    return _TEMPLATES.TemplateResponse(request, "fragments/source_menu.html", _status(request))
