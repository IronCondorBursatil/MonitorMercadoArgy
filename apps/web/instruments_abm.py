"""ABM (Alta/Baja/Modificación) de instrumentos — backend SQLite (§5.5).

CRUD sobre el catálogo SQLite vía SQLAlchemy (transaccional: `SessionLocal.begin()`
hace COMMIT/ROLLBACK automático). Reemplaza la antigua escritura a Excel
(openpyxl + `_LOCK` + `.tmp`/`os.replace`): el Excel quedó como pura semilla
(`scripts/ingest_master.py`), ya no se lee ni escribe en runtime.

`SHEET_SCHEMAS` (metadata de campos por hoja) sobrevive: el frontend la
introspecciona para renderizar el form correcto. Los valores crudos del form se
guardan en `InstrumentORM.raw_fields` (JSON) para que la edición haga round-trip
— el `Instrument` normalizado solo no alcanza para reconstruir los inputs (cupón,
schedule de amortización, etc. quedan horneados en los cashflows materializados).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from core.domain.models import Cashflow
from core.infrastructure.db.catalog_repository import init_db, instrument_to_orm
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import CashflowORM, InstrumentORM
from core.infrastructure.repositories import build_instrument

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Cashflows: parsing del input del frontend + síntesis desde params del form.
# --------------------------------------------------------------------------- #

def _synth_cashflows_for_fields(fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Genera cashflows on-the-fly desde un dict de fields del form ABM.

    Delegate al módulo puro `core.domain.cashflow_synth.synth_cashflows`.
    Devuelve dicts JSON-friendly (para el preview del form y el fallback de
    `get_instrument` cuando no hay cashflows materializados)."""
    return [
        {
            "date": cf.date.isoformat() if hasattr(cf.date, "isoformat") else str(cf.date),
            "amortization": float(cf.amortization),
            "interest": float(cf.interest),
        }
        for cf in _safe_synth(fields)
    ]


def _safe_synth(fields: Dict[str, Any]) -> List[Cashflow]:
    """synth_cashflows tolerante: input mal-formado → []. Bugs reales propagan."""
    try:
        from core.domain.cashflow_synth import synth_cashflows
        normalized = {str(k).lower().strip(): v for k, v in fields.items()}
        return list(synth_cashflows(normalized))
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        logger.warning(f"synth_cashflows invalid input: {e}")
        return []


def _parse_cashflows(cashflows: List[Dict[str, Any]]) -> List[tuple]:
    """Validar + parsear lista del frontend a tuples (date, amort, interest)
    ordenadas por fecha. Lanza ValueError en fecha inválida (fail-fast)."""
    parsed: List[tuple] = []
    for i, cf in enumerate(cashflows):
        d_raw = (cf.get("date") or "").strip()
        if not d_raw:
            continue
        try:
            d = datetime.strptime(d_raw[:10], "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(f"cashflow #{i+1}: invalid date {d_raw!r} (expected YYYY-MM-DD)")
        amort = float(cf.get("amortization") or 0)
        interest = float(cf.get("interest") or 0)
        parsed.append((d, amort, interest))
    parsed.sort(key=lambda x: x[0])
    return parsed


def _normalize_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Claves en minúscula/stripped; strings stripped (vacío → None); ticker upper."""
    out: Dict[str, Any] = {}
    for k, v in fields.items():
        key = str(k).lower().strip()
        if isinstance(v, str):
            v = v.strip() or None
        out[key] = v
    if out.get("ticker"):
        out["ticker"] = str(out["ticker"]).upper().strip()
    return out


# Per-sheet field metadata used by the frontend to render the right input
# widget. `key` matches the column header (lowercased + stripped) que también
# usa `build_instrument` y `synth_cashflows`.
_BASE_CALCULO_OPTIONS = ["", "ACT/365.25", "ACT/365", "30/360", "ACT/ACT"]
_TIPO_AMORT_OPTIONS  = ["", "bullet", "amortizing"]

SHEET_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "Soberanos": {
        "label": "Soberanos (BONAR / GLOBAL / BOPREAL)",
        "fields": [
            {"key": "ticker",          "label": "Ticker",               "type": "text",   "required": True,
             "help": "Símbolo en Data912, ej. AL30D"},
            {"key": "short_name",      "label": "Short name",           "type": "text",   "required": True},
            {"key": "tipo",            "label": "Tipo",                 "type": "select", "required": True,
             "options": ["BONAR", "GLOBAL", "BOPREAL"]},
            {"key": "fecha_emision",   "label": "Fecha emisión",        "type": "date",   "required": True,
             "help": "Origen del schedule de cupones"},
            {"key": "fecha_vencimiento","label": "Vencimiento",         "type": "date",   "required": True},
            {"key": "cupon anual %",   "label": "Cupón anual %",        "type": "text",   "required": True,
             "help": "Ej. 1.00 (decimal). Step-up: '2024-12-31:0.63;2027-12-31:1.18'"},
            {"key": "frecuencia pagos","label": "Frecuencia pagos /año","type": "number", "step": "1",
             "help": "2 = semestral (default AR), 4 = trimestral"},
            {"key": "base calculo",    "label": "Base cálculo",         "type": "select",
             "options": _BASE_CALCULO_OPTIONS,
             "help": "Convención day-count para TIR y duración. BOPREAL: 30/360"},
            {"key": "tipo amortizacion","label": "Tipo amortización",   "type": "select",
             "options": _TIPO_AMORT_OPTIONS},
            {"key": "amort inicio",    "label": "Amort inicio",         "type": "date",
             "help": "Primera fecha de amortización (solo amortizing)"},
            {"key": "amort cantidad",  "label": "Cant. cuotas amort.",  "type": "number", "step": "1",
             "help": "Ej. 13 cuotas semestrales → 1 cashflow por cuota"},
            {"key": "capital factor",  "label": "Capital factor",       "type": "number", "step": "0.0001",
             "help": "Solo bonos reestructurados (DICP/CUAP). Dejar vacío para el resto"},
        ],
    },
    "Tasa_Fija": {
        "label": "Tasa Fija (LECAP / BONCAP / BONOFIJA)",
        "fields": [
            {"key": "ticker",           "label": "Ticker",                "type": "text",   "required": True},
            {"key": "clase",            "label": "Clase",                 "type": "select", "required": True,
             "options": ["", "LECAP", "BONCAP", "BONOFIJA"]},
            # Comunes a todas las clases — solo aparecen una vez elegida la clase
            {"key": "fecha_emision",    "label": "Fecha emisión",         "type": "date",   "required": True,
             "help": "Para LECAP/BONCAP determina la capitalización",
             "classes": ["LECAP", "BONCAP", "BONOFIJA"]},
            {"key": "fecha_pago",       "label": "Vencimiento",           "type": "date",   "required": True,
             "classes": ["LECAP", "BONCAP", "BONOFIJA"]},
            # Solo LECAP / BONCAP
            {"key": "tem_licit",        "label": "TEM licitación",        "type": "number", "step": "0.0001",
             "help": "Decimal: 0.021 = 2.1%", "classes": ["LECAP", "BONCAP"]},
            # Solo BONOFIJA
            {"key": "cupon anual %",    "label": "Cupón anual %",         "type": "text",
             "help": "Ej. 2.50 (porcentual anual)", "classes": ["BONOFIJA"]},
            {"key": "frecuencia pagos", "label": "Frecuencia pagos /año", "type": "number", "step": "1",
             "help": "2 = semestral (default)", "classes": ["BONOFIJA"]},
            # Común — aparece después de elegir clase
            {"key": "base calculo",     "label": "Base cálculo",          "type": "select",
             "options": _BASE_CALCULO_OPTIONS,
             "help": "LECAP/BONCAP: 30/360 (Sec. Finanzas)",
             "classes": ["LECAP", "BONCAP", "BONOFIJA"]},
            # Solo BONOFIJA: amortización
            {"key": "tipo amortizacion","label": "Tipo amortización",     "type": "select",
             "options": _TIPO_AMORT_OPTIONS, "classes": ["BONOFIJA"]},
            {"key": "amort inicio",     "label": "Amort inicio",          "type": "date",
             "help": "Primera fecha de amortización",
             "classes": ["BONOFIJA"], "show_if_amort": True},
            {"key": "amort cantidad",   "label": "Cant. cuotas amort.",   "type": "number", "step": "1",
             "classes": ["BONOFIJA"], "show_if_amort": True},
        ],
    },
    "CER": {
        "label": "CER (LECER / BONCER / BONCER ZC / CON CUPON / STEP-UP)",
        "fields": [
            {"key": "ticker",          "label": "Ticker",               "type": "text",   "required": True},
            {"key": "tipo",            "label": "Tipo",                 "type": "select", "required": True,
             "options": ["LECER", "BONCER", "BONCER ZC", "CON CUPON", "STEP-UP"]},
            {"key": "fecha emision",   "label": "Fecha emisión",        "type": "date",   "required": True},
            {"key": "fecha vencimiento","label": "Vencimiento",         "type": "date",   "required": True},
            {"key": "cupon anual %",   "label": "Cupón anual %",        "type": "text",
             "help": "Ej. 5.83. Step-up: '2024-12-31:0.63;2027-12-31:1.18'"},
            {"key": "frecuencia pagos","label": "Frecuencia pagos /año","type": "number", "step": "1",
             "help": "2 = semestral, 4 = trimestral"},
            {"key": "base calculo",    "label": "Base cálculo",         "type": "select",
             "options": _BASE_CALCULO_OPTIONS},
            {"key": "tipo amortizacion","label": "Tipo amortización",   "type": "select",
             "options": _TIPO_AMORT_OPTIONS},
            {"key": "amort inicio",    "label": "Amort inicio",         "type": "date",
             "help": "Solo amortizing"},
            {"key": "amort cantidad",  "label": "Cant. cuotas amort.",  "type": "number", "step": "1"},
            {"key": "capital factor",  "label": "Capital factor",       "type": "number", "step": "0.0001",
             "help": "Capitalización inicial, ej. 1.27 para DICP"},
            {"key": "cer emision",     "label": "CER base (10h pre-emisión)", "type": "number",
             "step": "0.000001", "help": "Crítico: CER 10 días hábiles antes de emisión"},
            {"key": "categoria",       "label": "Categoría",            "type": "text",
             "help": "Ej. 'BONCERES CERO CUPON'"},
        ],
    },
    "Dolar_Linked": {
        "label": "Dolar Linked",
        "fields": [
            {"key": "ticker",          "label": "Ticker",               "type": "text",   "required": True},
            {"key": "fecha_emision",   "label": "Fecha emisión",        "type": "date",   "required": True},
            {"key": "fecha_vencimiento","label": "Vencimiento",         "type": "date",   "required": True},
            {"key": "cupon anual %",   "label": "Cupón anual %",        "type": "text",
             "help": "Vacío para zero-coupon (mayoría de los DL)"},
            {"key": "frecuencia pagos","label": "Frecuencia pagos /año","type": "number", "step": "1",
             "help": "Solo si paga cupones intermedios"},
            {"key": "base calculo",    "label": "Base cálculo",         "type": "select",
             "options": _BASE_CALCULO_OPTIONS},
            {"key": "tipo amortizacion","label": "Tipo amortización",   "type": "select",
             "options": _TIPO_AMORT_OPTIONS},
            {"key": "amort inicio",    "label": "Amort inicio",         "type": "date",
             "help": "Solo amortizing"},
            {"key": "amort cantidad",  "label": "Cant. cuotas amort.",  "type": "number", "step": "1"},
            {"key": "tc_inicial",      "label": "TC inicial",           "type": "number", "step": "0.0001",
             "help": "Tipo de cambio de emisión (pesos/USD)"},
        ],
    },
    "TAMAR": {
        "label": "TAMAR (PURO / DUAL / DUAL_CER_TAMAR)",
        "fields": [
            {"key": "ticker",          "label": "Ticker",               "type": "text",   "required": True},
            {"key": "tipo",            "label": "Tipo",                 "type": "select", "required": True,
             "options": ["PURO", "DUAL", "DUAL_CER_TAMAR"]},
            {"key": "fecha_emision",   "label": "Fecha emisión",        "type": "date",   "required": True},
            {"key": "fecha_vencimiento","label": "Vencimiento",         "type": "date",   "required": True},
            {"key": "cupon anual %",   "label": "Cupón anual % (si aplica)", "type": "text",
             "help": "Solo para DUAL con cupón fijo adicional"},
            {"key": "frecuencia pagos","label": "Frecuencia pagos /año","type": "number", "step": "1"},
            {"key": "base calculo",    "label": "Base cálculo",         "type": "select",
             "options": _BASE_CALCULO_OPTIONS,
             "help": "TAMAR usa 30/360 por documento BONTE"},
            {"key": "tipo amortizacion","label": "Tipo amortización",   "type": "select",
             "options": _TIPO_AMORT_OPTIONS},
            {"key": "amort inicio",    "label": "Amort inicio",         "type": "date"},
            {"key": "amort cantidad",  "label": "Cant. cuotas amort.",  "type": "number", "step": "1"},
            {"key": "tasa_fija_mensual","label": "Tasa fija mensual (decimal)", "type": "number",
             "step": "0.0001", "help": "Solo DUAL — el floor mensual"},
            {"key": "spread",          "label": "Spread TAMAR (decimal)","type": "number",
             "step": "0.0001", "help": "0.05 = TAMAR + 5%"},
            {"key": "cer_base",        "label": "CER base",             "type": "number", "step": "0.01",
             "help": "Solo DUAL_CER_TAMAR — CER 10h pre-emisión"},
            {"key": "cer_spread",      "label": "Spread CER (decimal)", "type": "number",
             "step": "0.0001", "help": "Solo DUAL_CER_TAMAR"},
        ],
    },
}


# --------------------------------------------------------------------------- #
# Lectura
# --------------------------------------------------------------------------- #

def list_instruments() -> List[Dict[str, str]]:
    """[{"ticker": "AL30D", "sheet": "Soberanos"}, ...] ordenado por ticker."""
    init_db()
    with SessionLocal() as s:
        rows = s.execute(
            select(InstrumentORM.ticker, InstrumentORM.sheet).order_by(InstrumentORM.ticker)
        ).all()
    return [{"ticker": t, "sheet": sh or ""} for t, sh in rows]


def get_instrument(ticker: str) -> Optional[Dict[str, Any]]:
    """{"sheet", "fields", "cashflows", "cashflows_source"} para un ticker, o None.

    `fields` son los params crudos del form (raw_fields). `cashflows_source` es
    "sheet" si hay cashflows materializados; si no, sintetiza desde los params y
    marca "synth" (o "empty" si tampoco se puede sintetizar)."""
    ticker_u = ticker.upper().strip()
    init_db()
    with SessionLocal() as s:
        orm = s.get(InstrumentORM, ticker_u)
        if orm is None:
            return None
        sheet = orm.sheet or ""
        fields = dict(orm.raw_fields or {})
        cf_rows = [
            {
                "date": cf.fecha_pago.isoformat(),
                "amortization": float(cf.amortizacion),
                "interest": float(cf.cupon_interes),
            }
            for cf in orm.cashflows
        ]
    if cf_rows:
        return {"sheet": sheet, "fields": fields, "cashflows": cf_rows, "cashflows_source": "sheet"}
    synth = _synth_cashflows_for_fields(fields)
    return {
        "sheet": sheet,
        "fields": fields,
        "cashflows": synth,
        "cashflows_source": "synth" if synth else "empty",
    }


# --------------------------------------------------------------------------- #
# Escritura (transaccional: SessionLocal.begin → COMMIT/ROLLBACK auto)
# --------------------------------------------------------------------------- #

def save_instrument(sheet: str, fields: Dict[str, Any],
                    cashflows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Alta/edición por ticker. Devuelve {"action": "created"|"updated", ...}.

    Cashflows:
    - `cashflows` provisto  → reemplaza los del ticker (source pasa a "sheet").
    - `cashflows=None`      → preserva los materializados existentes; si el ticker
                              es nuevo (o no tiene), sintetiza desde los params.

    Toda la operación (row + cashflows) ocurre en una sola transacción: si algo
    falla (p.ej. cashflow con fecha inválida) no persiste nada."""
    if sheet not in SHEET_SCHEMAS:
        raise ValueError(f"Unknown sheet '{sheet}'. Allowed: {list(SHEET_SCHEMAS)}")
    normalized = _normalize_fields(fields)
    ticker = str(normalized.get("ticker") or "").strip().upper()
    if not ticker:
        raise ValueError("ticker is required")

    # Validar cashflows ANTES de tocar la DB (fail-fast).
    parsed_cfs = _parse_cashflows(cashflows) if cashflows is not None else None

    init_db()
    with SessionLocal.begin() as s:
        existing = s.get(InstrumentORM, ticker)
        action = "updated" if existing is not None else "created"

        if parsed_cfs is not None:
            cfs = [Cashflow(date=d, amortization=a, interest=i) for d, a, i in parsed_cfs]
        elif existing is not None and existing.cashflows:
            cfs = [Cashflow(date=cf.fecha_pago, amortization=cf.amortizacion,
                            interest=cf.cupon_interes) for cf in existing.cashflows]
        else:
            cfs = _safe_synth(normalized)

        inst = build_instrument(normalized, sheet, cfs)
        if inst is None:
            raise ValueError("ticker is required")

        if existing is not None:
            s.delete(existing)
            s.flush()  # libera la PK antes de re-insertar
        s.add(instrument_to_orm(inst, sheet=sheet, raw_fields=normalized))

    logger.info("ABM: %s %s in %s%s", action, ticker, sheet,
                f" · {len(parsed_cfs)} cashflows" if parsed_cfs is not None else "")
    out: Dict[str, Any] = {"action": action, "ticker": ticker, "sheet": sheet}
    if parsed_cfs is not None:
        out["cashflows"] = len(parsed_cfs)
    return out


def save_cashflows(ticker: str, cashflows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Reemplaza TODAS las filas de cashflows del ticker (delete + insert)."""
    ticker_u = ticker.upper().strip()
    if not ticker_u:
        raise ValueError("ticker is required")
    parsed = _parse_cashflows(cashflows)
    init_db()
    with SessionLocal.begin() as s:
        orm = s.get(InstrumentORM, ticker_u)
        if orm is None:
            raise ValueError(f"{ticker_u} no existe")
        orm.cashflows = [
            CashflowORM(ticker=ticker_u, fecha_pago=d, amortizacion=a, cupon_interes=i)
            for d, a, i in parsed
        ]
    logger.info("ABM: saved %d cashflows for %s", len(parsed), ticker_u)
    return {"ticker": ticker_u, "count": len(parsed)}


def delete_instrument(ticker: str) -> Dict[str, str]:
    """Baja del ticker (cashflows en cascade). 'deleted' | 'not_found'."""
    ticker_u = ticker.upper().strip()
    if not ticker_u:
        raise ValueError("ticker is required")
    init_db()
    with SessionLocal.begin() as s:
        orm = s.get(InstrumentORM, ticker_u)
        existed = orm is not None
        sheet = orm.sheet if orm is not None else None
        if orm is not None:
            s.delete(orm)
    if not existed:
        return {"action": "not_found", "ticker": ticker_u}
    logger.info("ABM: deleted %s (%s)", ticker_u, sheet)
    return {"action": "deleted", "ticker": ticker_u, "sheet": sheet or ""}


def purge_matured_instruments() -> List[Dict[str, str]]:
    """Elimina instrumentos con vencimiento anterior a hoy. Devuelve las bajas."""
    today = date.today()
    init_db()
    deleted: List[Dict[str, str]] = []
    with SessionLocal.begin() as s:
        rows = s.execute(
            select(InstrumentORM).where(
                InstrumentORM.maturity_date.is_not(None),
                InstrumentORM.maturity_date < today,
            )
        ).scalars().all()
        for orm in rows:
            deleted.append({
                "ticker": orm.ticker,
                "sheet": orm.sheet or "",
                "maturity": orm.maturity_date.isoformat(),
            })
            s.delete(orm)
    for d in deleted:
        logger.info("Purge: eliminado %s (%s, vto %s)", d["ticker"], d["sheet"], d["maturity"])
    return deleted
