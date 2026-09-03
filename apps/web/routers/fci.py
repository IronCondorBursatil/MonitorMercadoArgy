"""Panel FCI (CAFCI) — app cliente + endpoint de datos.

GET /fci        → página (carga apps/web/static/js/fci.js, que hace fetch a /fci/data).
GET /fci/data   → dataset JSON `{meta, funds}` (armado en `apps/web/fci_service.py`:
                  CAFCI enriquecido + AUM ArgentinaDatos + lente A3500/CER + flujos
                  reales de fci_history). Memoizado por corte/día; servido con GZip.
"""
from __future__ import annotations

import gzip
import json
import threading

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from apps.web.deps import get_cafci, get_fx, get_indices
from apps.web.fci_service import get_fci_dataset
from apps.web.templates import TEMPLATES as _TEMPLATES

router = APIRouter()

# Bytes YA gzippeados del dataset, memoizados junto al dataset que los produjo. El
# dataset son ~4 MB de JSON: sin esto, CADA visita a /fci pagaba json.dumps (~92 ms) +
# gzip del middleware (~128 ms) aunque el dataset estuviera cacheado — y el
# GZipMiddleware comprime DENTRO del event loop, o sea que frenaba el SSE y todos los
# paneles.
#
# La clave es la IDENTIDAD del objeto (`is`), no `(generated_at, len(funds))`: ninguno
# de esos dos cambia entre un dataset degradado y uno sano (mismo corte CAFCI, misma
# cantidad de fondos), así que la clave vieja pisaba el guard deliberado de
# `fci_service` de "NO memoizar un dataset degradado" — el service reconstruía bien y
# el router seguía sirviendo los bytes sin AUM hasta el corte del día siguiente.
# `get_fci_dataset` devuelve EL MISMO dict mientras su cache sea válido (y uno nuevo
# cuando rebuildea: dataset degradado, rollover de día, corte nuevo, force=True), o sea
# que la identidad hereda exactamente su política de cacheabilidad.
_GZ_LOCK = threading.Lock()
_GZ: dict = {"src": None, "body": None}


@router.get("/fci", response_class=HTMLResponse)
def fci_page(request: Request):
    return _TEMPLATES.TemplateResponse(request, "pages/fci.html", {})


@router.get("/fci/data")
def fci_data(request: Request, cafci=Depends(get_cafci),
             indices=Depends(get_indices), fx=Depends(get_fx)):
    ds = get_fci_dataset(cafci, indices, fx)
    # Cliente sin gzip (raro: fci.js usa fetch, que siempre lo acepta) → camino normal.
    if "gzip" not in request.headers.get("accept-encoding", "").lower():
        return JSONResponse(ds)

    with _GZ_LOCK:
        body = _GZ["body"] if _GZ["src"] is ds else None
    if body is None:
        body = gzip.compress(
            json.dumps(ds, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            compresslevel=6)
        with _GZ_LOCK:
            _GZ["src"], _GZ["body"] = ds, body
    # Content-Encoding ya seteado → GZipMiddleware se saltea la respuesta (no
    # re-comprime): starlette/middleware/gzip.py chequea justamente ese header.
    return Response(content=body, media_type="application/json",
                    headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
