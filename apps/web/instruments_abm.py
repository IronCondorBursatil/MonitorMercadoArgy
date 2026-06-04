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
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from core.domain.models import Cashflow
from core.infrastructure.db.catalog_repository import init_db, instrument_to_orm
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import CashflowORM, InstrumentORM
from core.infrastructure.repositories import (
    build_instrument, _currency_tickers, split_currency_tickers,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Soberanos: un bono cotiza en 3 monedas con tickers distintos (mismo flujo,
# solo cambia el precio): ARS (sin sufijo) / MEP (sufijo D) / CABLE (sufijo C).
# El ABM los consolida en 1 sola entrada; el grupo se deriva del ticker por la
# convención de sufijo (la misma de `portfolio.position_currency`). No se guarda
# columna extra: las especies siguen siendo filas independientes (el pricing y
# los paneles no cambian), solo se las agrupa/orquesta en la capa ABM.
# --------------------------------------------------------------------------- #

_SOBERANOS_SHEET = "Soberanos"
# slot del form ↔ campo. El orden define la presentación (ARS, MEP, CABLE).
_SOB_SLOTS = ("ticker_ars", "ticker_mep", "ticker_ccl")

# Acciones: equities de Data912. Se registran solo con el ticker (sin términos
# ni flujos) bajo la categoría "Acciones" — no se editan como bonos ni aparecen
# en la lista de alta/edición; solo dejan de figurar en "sin cargar".
_ACCIONES_SHEET = "Acciones"
_ACCION_TYPE = "ACCION"


def _sob_group(ticker: str) -> str:
    """Base del bono = ticker sin el sufijo de moneda (D=MEP, C=CABLE).
    AL30/AL30D/AL30C → 'AL30'; AO27D → 'AO27'."""
    t = (ticker or "").upper().strip()
    return t[:-1] if (t and t[-1] in ("D", "C")) else t


def _sob_slot(ticker: str) -> str:
    """Campo del form que le corresponde a un ticker por su sufijo de moneda."""
    t = (ticker or "").upper().strip()
    if t.endswith("D"):
        return "ticker_mep"
    if t.endswith("C"):
        return "ticker_ccl"
    return "ticker_ars"


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
            # 3 tickers por moneda — mismo bono, solo cambia el precio. Al menos 1
            # requerido (se valida en save). Se atan/agrupan por el sufijo.
            {"key": "ticker_ars",      "label": "Ticker $ (ARS)",       "type": "text",
             "help": "Sin sufijo, ej. AL30. Vacío si no cotiza en pesos"},
            {"key": "ticker_mep",      "label": "Ticker MEP (D)",       "type": "text",
             "help": "Sufijo D, ej. AL30D"},
            {"key": "ticker_ccl",      "label": "Ticker CABLE (C)",     "type": "text",
             "help": "Sufijo C, ej. AL30C"},
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
    "Obligaciones_Negociables": {
        "label": "Obligaciones Negociables (ON USD · ley NY/ARG)",
        "fields": [
            {"key": "short_name",       "label": "Emisor",                "type": "text",   "required": True},
            {"key": "serie_clase",      "label": "Serie / Clase",         "type": "text",
             "help": "Etiqueta del informe IAMC/BYMA, ej. 'Clase XXXI' / 'Serie 13 Clase A'"},
            {"key": "ley_aplicable",    "label": "Ley Aplicable",         "type": "select",
             "options": ["", "Argentina", "Extranjera"],
             "help": "Ley de emisión: Argentina (ley local) o Extranjera (ley NY)"},
            {"key": "tipo",             "label": "Tipo",                  "type": "select", "required": True,
             "options": ["HARD DOLLAR", "DOLLAR LINKED"],
             "help": "Hard-dollar paga USD (pata …D); dollar-linked paga pesos × FX"},
            {"key": "fecha_emision",    "label": "Fecha emisión",         "type": "date",   "required": True},
            {"key": "fecha_vencimiento","label": "Vencimiento",           "type": "date",   "required": True},
            {"key": "cupon anual %",    "label": "Cupón anual %",         "type": "text",
             "help": "Tasa de cupón vigente, ej. 7.9. Vacío = cupón cero"},
            {"key": "frecuencia pagos", "label": "Frecuencia pagos /año", "type": "number", "step": "1",
             "help": "2 = semestral, 4 = trimestral, 1 = anual"},
            {"key": "prox_cupon",       "label": "Próximo / 1er cupón",   "type": "date",
             "help": "Solo si la grilla de cupones está desfasada de la emisión o hay 1er cupón largo/corto (ej. CS50)"},
            {"key": "base calculo",     "label": "Base cálculo",          "type": "select",
             "options": _BASE_CALCULO_OPTIONS, "help": "ON USD: real/365 = ACT/365 (default si se deja vacío)"},
            {"key": "tipo amortizacion","label": "Tipo amortización",     "type": "select",
             "options": _TIPO_AMORT_OPTIONS},
            {"key": "amort inicio",     "label": "Amort inicio (próx. pago capital)", "type": "date",
             "help": "Solo amortizing"},
            {"key": "amort cantidad",   "label": "Cuotas capital remanentes", "type": "number", "step": "1",
             "help": "Solo amortizing"},
            {"key": "capital factor",   "label": "Capital factor (VR/100)", "type": "number", "step": "0.0001",
             "help": "VR < 100 → ej. 0.40 si el valor residual es 40"},
        ],
    },
}


# Multi-ticker para TODAS las hojas: hasta 3 tickers por moneda (≥1). La hoja
# Soberanos ya trae los slots; al resto les anteponemos estos y quitamos el campo
# `ticker` único. La moneda se deriva del sufijo (D=MEP, C=CABLE).
_CCY_TICKER_FIELDS = [
    {"key": "ticker_ars", "label": "Ticker $ (pesos)", "type": "text",
     "help": "Ticker en pesos / principal. Al menos 1 de los 3 es obligatorio"},
    {"key": "ticker_mep", "label": "Ticker MEP (D)", "type": "text",
     "help": "Sufijo D — opcional"},
    {"key": "ticker_ccl", "label": "Ticker CABLE (C)", "type": "text",
     "help": "Sufijo C — opcional"},
]
for _schema in SHEET_SCHEMAS.values():
    _flds = _schema["fields"]
    if not any(f["key"] == "ticker_ars" for f in _flds):
        _schema["fields"] = _CCY_TICKER_FIELDS + [f for f in _flds if f["key"] != "ticker"]


# --------------------------------------------------------------------------- #
# Lectura
# --------------------------------------------------------------------------- #

def _row_tickers(orm: InstrumentORM) -> List[str]:
    """Tickers no vacíos de una fila-bono: primario + patas de moneda."""
    return [t for t in (orm.ticker, orm.ticker_mep, orm.ticker_ccl) if t]


def list_instruments() -> List[Dict[str, Any]]:
    """Entradas para la lista del ABM, 1 por bono (cada fila ya es un instrumento
    con hasta 3 tickers). {"sheet", "key"=primario, "display"='T / TD / TC', "tickers"}.
    Excluye las Acciones (solo-ticker, no se editan como bonos)."""
    init_db()
    with SessionLocal() as s:
        rows = s.execute(select(InstrumentORM).order_by(InstrumentORM.ticker)).scalars().all()
        out = [{"sheet": o.sheet or "", "key": o.ticker,
                "display": " / ".join(_row_tickers(o)), "tickers": _row_tickers(o)}
               for o in rows if o.sheet != _ACCIONES_SHEET]
    out.sort(key=lambda e: e["key"])
    return out


def register_stocks(tickers) -> List[str]:
    """Da de alta acciones (equities) con SOLO el ticker (sin términos ni flujos),
    bajo la categoría 'Acciones'. Idempotente — no toca las ya presentes (ni las
    que ya son tickers de otro instrumento). Devuelve los tickers agregados.

    Escribe SQLite directo (no el Excel) → se re-aplica al arranque. Al quedar en
    el catálogo, dejan de figurar en el listado 'sin cargar' de Data912."""
    syms = {str(t).upper().strip() for t in tickers if t and str(t).strip()}
    init_db()
    added: List[str] = []
    with SessionLocal.begin() as s:
        rows = s.execute(select(InstrumentORM)).scalars().all()
        present = {t.upper() for o in rows for t in _row_tickers(o)}
        for sym in sorted(syms):
            if sym in present:
                continue
            s.add(InstrumentORM(ticker=sym, short_name=sym,
                                instrument_type=_ACCION_TYPE, sheet=_ACCIONES_SHEET))
            present.add(sym)
            added.append(sym)
    if added:
        logger.info("Acciones: +%d dadas de alta (%s%s)", len(added),
                    ", ".join(added[:8]), "…" if len(added) > 8 else "")
    return added


def _find_bond_row(s, ticker_u: str) -> Optional[InstrumentORM]:
    """Fila-bono que contiene `ticker_u` en cualquier slot (primario/mep/ccl)."""
    orm = s.get(InstrumentORM, ticker_u)
    if orm is not None:
        return orm
    rows = s.execute(select(InstrumentORM)).scalars().all()
    return next((o for o in rows
                 if (o.ticker_mep and o.ticker_mep.upper() == ticker_u)
                 or (o.ticker_ccl and o.ticker_ccl.upper() == ticker_u)), None)


def get_instrument(ticker: str) -> Optional[Dict[str, Any]]:
    """{"sheet", "fields", "cashflows", "cashflows_source"} para CUALQUIER ticker
    del bono (primario o pata), o None. `fields` trae los slots ticker_ars/mep/ccl
    reconstruidos desde la fila (autoritativo)."""
    ticker_u = ticker.upper().strip()
    init_db()
    with SessionLocal() as s:
        orm = _find_bond_row(s, ticker_u)
        if orm is None:
            return None
        sheet = orm.sheet or ""
        fields = dict(orm.raw_fields or {})
        # los slots de ticker reflejan la fila (no los raw_fields, que pueden
        # estar viejos): clasificar cada ticker por sufijo.
        for k in ("ticker", *_SOB_SLOTS):
            fields.pop(k, None)
        for tk in _row_tickers(orm):
            fields[_sob_slot(tk)] = tk
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

def _find_bond_rows(s, tickers) -> List[InstrumentORM]:
    """Filas cuyo ticker primario o pata (mep/ccl) esté en `tickers`. Incluye
    filas-por-pata pre-migración (para consolidarlas al guardar)."""
    ts = {str(t).upper().strip() for t in tickers if t}
    if not ts:
        return []
    rows = s.execute(select(InstrumentORM)).scalars().all()
    return [o for o in rows
            if o.ticker.upper() in ts
            or (o.ticker_mep and o.ticker_mep.upper() in ts)
            or (o.ticker_ccl and o.ticker_ccl.upper() in ts)]


def save_instrument(sheet: str, fields: Dict[str, Any],
                    cashflows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Alta/edición de un instrumento (1 fila por bono, hasta 3 tickers por moneda).

    Lee los tickers del form (ticker_ars/ticker_mep/ticker_ccl, o `ticker` único)
    → escribe UNA fila con primario + ticker_mep/ticker_ccl, consolidando cualquier
    fila-por-pata pre-existente. Cashflows: explícitos > los del bono existente >
    synth. Todo en una transacción (fail-fast)."""
    if sheet not in SHEET_SCHEMAS:
        raise ValueError(f"Unknown sheet '{sheet}'. Allowed: {list(SHEET_SCHEMAS)}")
    normalized = _normalize_fields(fields)
    tickers = _currency_tickers(normalized)
    if not tickers:
        raise ValueError("se requiere al menos un ticker (pesos / MEP / CABLE)")
    primary, mep, ccl = split_currency_tickers(tickers)
    all_tickers = [t for t in (primary, mep, ccl) if t]

    parsed_cfs = _parse_cashflows(cashflows) if cashflows is not None else None

    init_db()
    with SessionLocal.begin() as s:
        existing = _find_bond_rows(s, tickers + all_tickers)
        action = "updated" if existing else "created"

        if parsed_cfs is not None:
            cfs = [Cashflow(date=d, amortization=a, interest=i) for d, a, i in parsed_cfs]
        else:
            old = next((o for o in existing if o.cashflows), None)
            if old is not None:
                cfs = [Cashflow(date=cf.fecha_pago, amortization=cf.amortizacion,
                                interest=cf.cupon_interes) for cf in old.cashflows]
            else:
                cfs = _safe_synth(normalized)

        for o in existing:
            s.delete(o)
        s.flush()  # libera las PK antes de re-insertar

        inst = build_instrument(normalized, sheet, cfs)  # ticker = primario
        if inst is None:
            raise ValueError("ticker is required")
        s.add(instrument_to_orm(inst, sheet=sheet, raw_fields=normalized,
                                ticker_mep=mep, ticker_ccl=ccl))

    logger.info("ABM: %s %s [%s] in %s%s", action, primary, ",".join(all_tickers), sheet,
                f" · {len(parsed_cfs)} cashflows" if parsed_cfs is not None else "")
    out: Dict[str, Any] = {"action": action, "ticker": primary, "sheet": sheet,
                           "tickers": all_tickers}
    if parsed_cfs is not None:
        out["cashflows"] = len(parsed_cfs)
    return out


def save_cashflows(ticker: str, cashflows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Reemplaza los cashflows del BONO que contiene `ticker` (primario o pata).
    Los flujos son del bono (compartidos por sus monedas) → se guardan bajo el
    ticker primario. delete + insert."""
    ticker_u = ticker.upper().strip()
    if not ticker_u:
        raise ValueError("ticker is required")
    parsed = _parse_cashflows(cashflows)
    init_db()
    with SessionLocal.begin() as s:
        orm = _find_bond_row(s, ticker_u)
        if orm is None:
            raise ValueError(f"{ticker_u} no existe")
        orm.cashflows = [
            CashflowORM(ticker=orm.ticker, fecha_pago=d, amortizacion=a, cupon_interes=i)
            for d, a, i in parsed
        ]
    logger.info("ABM: saved %d cashflows for %s (bono %s)", len(parsed), ticker_u, orm.ticker)
    return {"ticker": ticker_u, "count": len(parsed)}


def delete_instrument(key: str) -> Dict[str, Any]:
    """Baja del bono que contiene `key` en cualquier slot (primario o pata de
    moneda). Borra la fila (todas sus patas) + cashflows en cascade. Devuelve los
    tickers borrados. 'deleted' | 'not_found'."""
    k = key.upper().strip()
    if not k:
        raise ValueError("ticker is required")
    init_db()
    with SessionLocal.begin() as s:
        orm = _find_bond_row(s, k)
        if orm is None:
            return {"action": "not_found", "ticker": k}
        deleted = _row_tickers(orm)
        sheet = orm.sheet
        s.delete(orm)
    logger.info("ABM: deleted %s (%s)", ",".join(deleted), sheet)
    return {"action": "deleted", "ticker": k, "sheet": sheet or "", "tickers": deleted}


def backfill_soberano_ccy_legs(market_symbols) -> List[str]:
    """Completa las patas de moneda (D=MEP, C=CABLE, o ARS sin sufijo) de soberanos
    YA cargados que cotizan en Data912 pero faltan: las setea en los slots
    ticker_mep/ticker_ccl de la MISMA fila-bono (mismo instrumento, no filas nuevas).

    NO inventa bonos nuevos: si el grupo base no está cargado, lo ignora (eso se da
    de alta a mano). Idempotente. Devuelve los tickers agregados."""
    syms = {str(s).upper().strip() for s in market_symbols}
    init_db()
    added: List[str] = []
    with SessionLocal.begin() as s:
        rows = s.execute(
            select(InstrumentORM).where(InstrumentORM.sheet == _SOBERANOS_SHEET)
        ).scalars().all()
        present = {t.upper() for o in rows for t in _row_tickers(o)}
        by_group: Dict[str, InstrumentORM] = {}
        for o in rows:
            by_group.setdefault(_sob_group(o.ticker), o)

        for sym in sorted(syms):
            if sym in present:
                continue
            o = by_group.get(_sob_group(sym))
            if o is None:                       # bono base no cargado → no backfill
                continue
            # poné sym en su slot por sufijo si está libre, si no en cualquiera libre
            for slot in (_sob_slot(sym), "ticker_mep", "ticker_ccl"):
                if slot in ("ticker_mep", "ticker_ccl") and not getattr(o, slot):
                    setattr(o, slot, sym)
                    added.append(sym)
                    present.add(sym)
                    break
    if added:
        logger.info("Backfill soberanos: +%d patas de moneda %s", len(added), added)
    return added


def unknown_data912_tickers(
    snapshot: Dict[str, Any],
    sources: Dict[str, str],
    catalog_tickers,
    exclude=frozenset(),
) -> Dict[str, List[Dict[str, Any]]]:
    """Símbolos que cotizan en Data912 (snapshot del hub) pero NO están en el
    catálogo, agrupados por endpoint de origen. Es el listado de referencia del
    sidebar del ABM: lo que falta dar de alta.

    - `snapshot`: {symbol: Data912Row}  (del ProviderHub)
    - `sources`:  {symbol: endpoint}     ('notes'/'bonds'/'corp'/'stocks')
    - `catalog_tickers`: tickers ya cargados; se normaliza el alias `_CER` al
      símbolo de mercado (TXMJ8_CER → TXMJ8) para no marcar duales como faltantes.
    - `exclude`: símbolos a omitir (p.ej. PANEL_LIDER, ya mostrados en su panel).

    Devuelve {endpoint: [{"ticker", "price"}, ...]} ordenado por ticker.
    """
    def _norm(t: str) -> str:
        tu = str(t).upper().strip()
        return tu[:-4] if tu.endswith("_CER") else tu

    cat = {_norm(t) for t in catalog_tickers}
    ex = {str(e).upper().strip() for e in exclude}
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for sym, row in snapshot.items():
        symu = str(sym).upper().strip()
        if symu in cat or symu in ex:
            continue
        src = sources.get(sym) or sources.get(symu) or "otros"
        groups.setdefault(src, []).append(
            {"ticker": symu, "price": getattr(row, "c", None)}
        )
    for rows in groups.values():
        rows.sort(key=lambda r: r["ticker"])
    return groups


_CORP_SUFFIX_ORDER = {"O": 0, "D": 1, "C": 2}


def group_corp_tickers(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Agrupa tickers de ONs por base (todo menos el último char) cuando el
    sufijo es O (pesos) / D (MEP) / C (cable). Los que no siguen el patrón
    se muestran solos al final.

    Devuelve [{"base": str, "variants": [{"ticker", "price", "suffix"}, ...]}, ...]
    """
    bases: Dict[str, Dict[str, Dict]] = {}
    unmatched: List[Dict] = []
    for it in items:
        tk = it["ticker"]
        suffix = tk[-1] if tk else ""
        if suffix in _CORP_SUFFIX_ORDER and len(tk) > 1:
            base = tk[:-1]
            bases.setdefault(base, {})[suffix] = {**it, "suffix": suffix}
        else:
            unmatched.append({**it, "suffix": ""})

    result = []
    for base in sorted(bases):
        variants = [
            bases[base][s]
            for s in ("O", "D", "C")
            if s in bases[base]
        ]
        result.append({"base": base, "variants": variants})

    for it in sorted(unmatched, key=lambda x: x["ticker"]):
        result.append({"base": it["ticker"], "variants": [it]})

    return result
