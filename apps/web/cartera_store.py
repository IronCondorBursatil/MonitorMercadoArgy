"""Store de tenencias de la cartera (data/cartera.json).

Persistencia simple para el módulo Cartera: una lista de tenencias
{ticker, nominal, cost_price, note, added}. Desacoplado del master de
instrumentos (las tenencias NO son instrumentos, así que no van al Excel
maestro). Escritura atómica (.tmp + os.replace) y RLock propio, mismo criterio
de resiliencia que instruments_abm.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from datetime import date
from typing import Dict, List, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_PATH = str(settings.cartera_json)
# Ubicación VIEJA (dentro del working tree). Se migra sola en la primera lectura:
# sin eso, al mover la ruta el usuario abre el panel y no tiene tenencias.
_LEGACY_PATH = os.path.join(str(settings.data_dir), "cartera.json")


def _migrar_legacy_si_hace_falta() -> None:
    """Copia la cartera vieja a la ubicación nueva, 1×, si la nueva no existe.

    COPIA y no mueve a propósito: si hay que volver a una versión anterior del código,
    esa versión busca el archivo donde estaba y tiene que encontrarlo. El costo es un
    JSON duplicado de unos KB hasta que alguien borre el viejo."""
    if os.path.isfile(_PATH) or not os.path.isfile(_LEGACY_PATH):
        return
    try:
        os.makedirs(os.path.dirname(_PATH) or ".", exist_ok=True)
        shutil.copy2(_LEGACY_PATH, _PATH)
        logger.info("cartera: migrada de %s a %s", _LEGACY_PATH, _PATH)
    except OSError as e:
        logger.warning("no se pudo migrar la cartera desde %s (%s)", _LEGACY_PATH, e)


def _read() -> List[dict]:
    _migrar_legacy_si_hace_falta()
    if not os.path.isfile(_PATH):
        return []
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("cartera.json ilegible (%s) — arranco vacío", e)
        return []
    holdings = data.get("holdings") if isinstance(data, dict) else data
    return holdings if isinstance(holdings, list) else []


def _write(holdings: List[dict]) -> None:
    """Atomic: escribe a .tmp y os.replace. Sin esto un crash mid-write dejaría
    el JSON truncado y el siguiente boot perdería la cartera."""
    tmp = _PATH + ".tmp"
    payload = {"holdings": holdings, "updated": date.today().isoformat()}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _PATH)


def list_holdings() -> List[dict]:
    with _LOCK:
        return _read()


def upsert_holding(ticker: str, nominal, cost_price=None, note: str = "") -> Dict[str, str]:
    """Crea o actualiza una tenencia por ticker. Devuelve {action, ticker}."""
    tk = str(ticker or "").upper().strip()
    if not tk:
        raise ValueError("ticker is required")
    try:
        nominal = float(nominal)
    except (TypeError, ValueError):
        raise ValueError("nominal must be a number")
    if nominal <= 0:
        raise ValueError("nominal must be > 0")
    cp: Optional[float]
    try:
        cp = float(cost_price) if cost_price not in (None, "") else None
    except (TypeError, ValueError):
        raise ValueError("cost_price must be a number or empty")

    with _LOCK:
        holdings = _read()
        existing = next((h for h in holdings if str(h.get("ticker", "")).upper() == tk), None)
        action = "updated" if existing else "created"
        rec = {
            "ticker": tk,
            "nominal": nominal,
            "cost_price": cp,
            "note": str(note or "").strip(),
            "added": (existing or {}).get("added") or date.today().isoformat(),
        }
        if existing:
            holdings = [rec if str(h.get("ticker", "")).upper() == tk else h for h in holdings]
        else:
            holdings.append(rec)
        _write(holdings)
    logger.info("Cartera: %s %s (VN %s)", action, tk, nominal)
    return {"action": action, "ticker": tk}


def delete_holding(ticker: str) -> Dict[str, str]:
    tk = str(ticker or "").upper().strip()
    if not tk:
        raise ValueError("ticker is required")
    with _LOCK:
        holdings = _read()
        kept = [h for h in holdings if str(h.get("ticker", "")).upper() != tk]
        if len(kept) == len(holdings):
            return {"action": "not_found", "ticker": tk}
        _write(kept)
    logger.info("Cartera: deleted %s", tk)
    return {"action": "deleted", "ticker": tk}
