"""Mapeo puro fila BYMA → `Data912Row` (el shape que consume el resto del sistema).

Los nombres de campo son los mismos en la API open y en la realtime (mismo backend
`vanoms-be-core`); ver el diccionario completo en `BYMADATA_EXCEL/GUIA_BYMADATA.md §2`.
Función pura y testeable sin red."""

from __future__ import annotations

import math
from typing import Optional

from core.infrastructure.schemas import Data912Row

# settlementType numérico de la API → etiqueta (T+2/48hs ya no existe en BYMA).
SETTLE_LABEL = {1: "CI", 2: "24hs", "1": "CI", "2": "24hs"}

# optionType (panel /options) → kind interno del parser de opciones (C/V).
_OPT_KIND = {"CALL": "C", "PUT": "V", "C": "C", "V": "V", "P": "V"}

# Plazos canónicos del proyecto.
SETTLE_24 = "24"   # 24hs (T1, settlementType 2) — el default
SETTLE_CI = "CI"   # contado inmediato (T0, settlementType 1)


def settle_of(raw: dict) -> str:
    """Plazo de una fila BYMA por su `settlementType` (1=CI, 2/otros=24hs)."""
    return SETTLE_CI if str((raw or {}).get("settlementType")) == "1" else SETTLE_24


def _f(v) -> Optional[float]:
    """Float o None (celdas vacías/no numéricas → None)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def byma_row_to_quote(raw: dict) -> Optional[Data912Row]:
    """Fila cruda de un panel BYMA (open o realtime) → `Data912Row`, o None si no
    hay símbolo / la fila es inválida.

    Mapeo (campo BYMA → Data912Row):
      trade→c, bidPrice→px_bid, offerPrice→px_ask, volumeAmount→v,
      numberOfOrders→q_op, imbalance(fracción)→pct_change(%)."""
    if not isinstance(raw, dict):
        return None
    sym = raw.get("symbol")
    if not sym:
        return None

    last = _f(raw.get("trade"))
    close = _f(raw.get("closingPrice"))
    prev_close = _f(raw.get("previousClosingPrice"))
    imb = _f(raw.get("imbalance"))
    n_ops = raw.get("numberOfOrders")
    try:
        q_op = int(round(float(n_ops))) if n_ops not in (None, "") else None
    except (TypeError, ValueError):
        q_op = None

    # Metadata de opciones (solo en el panel /options; ausente/None en el resto).
    und = raw.get("underlyingSymbol")
    mat = raw.get("maturityDate")
    opt_kind = _OPT_KIND.get(str(raw.get("optionType") or "").upper()) or None

    try:
        return Data912Row(
            symbol=str(sym),
            # `c` = LAST (trade, último operado). Si NO operó en la rueda (trade 0/None),
            # caemos al CIERRE (closingPrice) y luego al CIERRE ANTERIOR (previousClosingPrice):
            # así las especies sin operaciones igual aparecen con precio de mercado (todo el
            # universo, p.ej. ONs ilíquidas), en vez de quedar en 0.00 y ocultarse donde se
            # exige precio (panel ON). Sin ningún precio → 0.0 (Field ge=0).
            # Guard de finitud: BYMA puede enviar NaN/Inf; el validator de Data912Row lo
            # rechazará fila por fila antes de que llegue al motor/store.
            c=next((p for p in (last, close, prev_close)
                    if p is not None and p > 0 and math.isfinite(p)), 0.0),
            px_bid=_f(raw.get("bidPrice")),
            px_ask=_f(raw.get("offerPrice")),
            v=_f(raw.get("volumeAmount")),
            q_op=q_op,
            # imbalance es FRACCIÓN (-0.0356 = -3,56%). Lo pasamos a % para igualar
            # la convención de Data912 `pct_change`. (display-only; bajo riesgo.)
            pct_change=imb * 100.0 if imb is not None else None,
            oi=_f(raw.get("openInterest")),
            opt_kind=opt_kind,
            opt_underlying=str(und).upper().strip() if und else None,
            opt_expiry=str(mat)[:10] if mat else None,
        )
    except Exception:  # noqa: BLE001 — fila corrupta no debe tumbar el batch
        return None
