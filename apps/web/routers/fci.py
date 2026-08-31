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

# Bytes YA gzippeados del dataset, memoizados junto a su identidad. El dataset son
# ~4 MB de JSON: sin esto, CADA visita a /fci pagaba json.dumps (~92 ms) + gzip del
# middleware (~128 ms) aunque el dataset estuviera cacheado — y el GZipMiddleware
# comprime DENTRO del event loop, o sea que frenaba el SSE y todos los paneles.
# Se memoiza por el mismo `generated_at` del dataset, así se invalida solo.
_GZ_LOCK = threading.Lock()
_GZ: dict = {"key": None, "body": None}


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

    key = ((ds.get("meta") or {}).get("generated_at"), len(ds.get("funds") or ()))
    with _GZ_LOCK:
        body = _GZ["body"] if _GZ["key"] == key else None
    if body is None:
        body = gzip.compress(
            json.dumps(ds, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            compresslevel=6)
        with _GZ_LOCK:
            _GZ["key"], _GZ["body"] = key, body
    # Content-Encoding ya seteado → GZipMiddleware se saltea la respuesta (no
    # re-comprime): starlette/middleware/gzip.py chequea justamente ese header.
    return Response(content=body, media_type="application/json",
                    headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
