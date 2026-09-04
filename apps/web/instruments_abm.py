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

from sqlalchemy import or_, select

from core.domain.currency import ccy_from_suffix
from core.domain.models import Cashflow
from core.domain.on_classification import SECTORS as _ON_SECTORS, SECTOR_MAP, classify_sector
from core.infrastructure.db.catalog_repository import init_db, instrument_to_orm
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import CashflowORM, InstrumentORM
from core.domain.instrument_groups import has_closed_form_payoff, is_known_type
from core.infrastructure.repositories import (
    build_instrument, _currency_tickers, split_currency_tickers,
    _resolve_instrument_type, audit_catalog_types,
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
    ccy = ccy_from_suffix(ticker)
    if ccy == "MEP":
        return "ticker_mep"
    if ccy == "CABLE":
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


_NOTA_ANALITICO = ("Este tipo cobra por fórmula cerrada (TAMAR observada + proyectada): "
                   "no lleva schedule. Al guardar queda sólo la fila ancla del vencimiento.")


def preview_cashflows(fields: Dict[str, Any], sheet: str = "") -> Dict[str, Any]:
    """{"cashflows": [...], "nota": str} — schedule PROPUESTO desde los params del form.
    **No persiste nada.**

    Es el único consumidor legítimo del synth desde la ABM: el operador lo revisa en la
    tabla del cajón y el POST de `/abm/save` lo manda de vuelta. Ver el docstring de
    `save_instrument` — la síntesis lee el reloj (step-up del cupón), así que fuera del
    write-path es una propuesta reproducible y adentro era un schedule que dependía del
    día del alta.

    Con `sheet` aplica la MISMA regla por tipo que el save: a un payoff cerrado no se le
    propone nada, porque `save_instrument` lo descartaría — proponerlo es ofrecerle al
    operador trabajo que se va a tirar. `nota` dice por qué la tabla quedó vacía; sin
    ella el botón «⟳ Previsualizar» se ve como si no hubiera hecho nada.

    NUNCA lanza por el tipo: es una vista previa, no un borde de escritura. Si el tipo no
    se puede resolver, cae al synth y el rechazo (o no) lo decide el save."""
    try:
        normalized = _normalize_fields(fields)
        primary = split_currency_tickers(_currency_tickers(normalized))[0]
        itype = _resolve_instrument_type(normalized, sheet, primary, warn=False)
    except Exception:                       # noqa: BLE001 — preview tolerante (ver arriba)
        itype = None
    if itype is not None and has_closed_form_payoff(itype):
        return {"cashflows": [], "nota": _NOTA_ANALITICO}
    return {"cashflows": _synth_cashflows_for_fields(fields), "nota": ""}


def _safe_synth(fields: Dict[str, Any]) -> List[Cashflow]:
    """synth_cashflows tolerante: input mal-formado → []. Bugs reales propagan.

    Captura SOLO las excepciones de input sucio del form (valor no parseable,
    clave faltante, tipo incompatible). `AttributeError` NO se atrapa a propósito:
    casi siempre es un bug DENTRO de synth_cashflows, y tragarlo guardaría un alta
    con cero cashflows sin aviso — choca con la política 'NUNCA tragar el error'."""
    try:
        from core.domain.cashflow_synth import synth_cashflows
        normalized = {str(k).lower().strip(): v for k, v in fields.items()}
        return list(synth_cashflows(normalized))
    except (ValueError, KeyError, TypeError) as e:
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
# Categoría/sector para ordenar una ON a mano (override del match por emisor). "" = auto.
# El value es la key canónica (lo que matchea sector_for); la etiqueta visible es la
# MISMA etiqueta corta que usa el monitor de ON (Real Estate / Energía / Serv. Financieros…).
_CATEGORIA_OPTIONS = [""] + [s.key for s in _ON_SECTORS]
_CATEGORIA_LABELS = {"": "—", **{s.key: s.short for s in _ON_SECTORS}}

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
            {"key": "sector_override",  "label": "Categoría (sector)",    "type": "select",
             "options": _CATEGORIA_OPTIONS, "opt_labels": _CATEGORIA_LABELS,
             "help": "Sector donde se ordena la ON. Vacío = se deduce del emisor"},
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
            {"key": "base calculo",     "label": "Base cálculo",          "type": "select",
             "options": _BASE_CALCULO_OPTIONS, "help": "ON USD: real/365 = ACT/365 (default si se deja vacío)"},
            {"key": "tipo amortizacion","label": "Tipo amortización",     "type": "select",
             "options": _TIPO_AMORT_OPTIONS},
            # Denominación (referencia BYMA; display-only, no entra al pricing)
            {"key": "denom_base",       "label": "Denominación base",     "type": "number", "step": "0.01",
             "help": "Denominación mínima (BYMA), ej. 1.00"},
            {"key": "denom_incremento", "label": "Incrementos",           "type": "number", "step": "0.01",
             "help": "Incremento de denominación (BYMA), ej. 1.00"},
            {"key": "valor_nominal",    "label": "Valor nominal",         "type": "number", "step": "0.01",
             "help": "Valor nominal unitario (BYMA), ej. 1.00"},
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

# ISIN: campo común a todas las hojas (se enriquece solo desde BYMA, editable a mano).
_ISIN_FIELD = {"key": "isin", "label": "ISIN", "type": "text",
               "help": "Clave del activo (BYMA). Se completa solo; editable a mano"}
for _schema in SHEET_SCHEMAS.values():
    _flds = _schema["fields"]
    if not any(f["key"] == "ticker_ars" for f in _flds):
        _flds = _CCY_TICKER_FIELDS + [f for f in _flds if f["key"] != "ticker"]
    if not any(f["key"] == "isin" for f in _flds):
        _flds = _flds + [_ISIN_FIELD]
    _schema["fields"] = _flds


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
        out = []
        for o in rows:
            if o.sheet == _ACCIONES_SHEET:
                continue
            # Columnas ON (filtrables en el ABM): ticker pesos (=primario), Ley (AR/EXT),
            # Tipo (HD/DL) y Amortización (bullet/amortizing), derivadas de raw_fields /
            # instrument_type. Vacías para las hojas no-ON (no muestran esas columnas).
            rf = o.raw_fields or {}
            itype = (rf.get("tipo") or o.instrument_type or "").upper()
            tipo = "HD" if "HARD DOLLAR" in itype else (
                "DL" if ("DOLLAR LINKED" in itype or "DOLAR LINKED" in itype) else "")
            leyr = (rf.get("ley_aplicable") or "").upper()
            ley = "AR" if "ARGENTIN" in leyr else ("EXT" if "EXTRANJ" in leyr else "")
            amort = (rf.get("tipo amortizacion") or "").strip().lower()
            if amort not in ("bullet", "amortizing"):
                amort = ""
            out.append({"sheet": o.sheet or "", "key": o.ticker,
                        "display": " / ".join(_row_tickers(o)), "tickers": _row_tickers(o),
                        "peso": o.ticker, "emisor": o.short_name or "",
                        "ley": ley, "tipo": tipo, "amort": amort})
    out.sort(key=lambda e: e["key"])
    return out


# --------------------------------------------------------------------------- #
# Completitud: tabla "adaptativa por hoja" del ABM — por instrumento, qué campos
# relevantes están llenos (auditoría de carga). Las columnas salen del schema de
# la hoja (SHEET_SCHEMAS) menos las patas de ticker y la denominación display-only.
# --------------------------------------------------------------------------- #

_COV_SKIP = frozenset({"ticker_ars", "ticker_mep", "ticker_ccl",
                       "denom_base", "denom_incremento", "valor_nominal"})


def coverage_columns(sheet: str) -> List[Dict[str, Any]]:
    """Columnas de datos para la tabla de completitud de una hoja: los campos del
    form (SHEET_SCHEMAS) menos las patas de ticker y la denominación (display-only).
    El ticker primario, Cashflows y Precio los renderiza la plantilla aparte."""
    flds = SHEET_SCHEMAS.get(sheet, {}).get("fields", [])
    return [{"key": f["key"], "label": f["label"], "required": bool(f.get("required"))}
            for f in flds if f["key"] not in _COV_SKIP]


def list_instruments_coverage(price_of=None, sheet: Optional[str] = None) -> List[Dict[str, Any]]:
    """Por instrumento (1 fila por bono), el estado de carga de sus campos relevantes:
    valores, flags de faltantes, # de cashflows, precio vivo y % de completitud.

    El % cuenta los campos de datos de la hoja + cashflows (requerido); el **precio**
    NO entra en el % (viene del feed, no es carga manual) pero se expone como columna.
    `price_of(ticker)->float|None` (ej. `AppState.price_of`) da el precio vivo.
    Ordena los menos completos primero (auditoría). Excluye Acciones y hojas
    desconocidas. Si `sheet` se pasa, filtra a esa hoja."""
    from sqlalchemy import func
    from sqlalchemy.orm import noload
    init_db()
    with SessionLocal() as s:
        # noload: la tabla de completitud solo necesita raw_fields/sheet, no los cashflows.
        # cfcounts viene de una query de conteo separada — más rápida que cargar todos los flujos.
        rows = s.execute(select(InstrumentORM).options(noload(InstrumentORM.cashflows))).scalars().all()
        cfcounts = dict(s.execute(
            select(CashflowORM.ticker, func.count()).group_by(CashflowORM.ticker)).all())

    out: List[Dict[str, Any]] = []
    for o in rows:
        sh = o.sheet or ""
        if sh == _ACCIONES_SHEET or sh not in SHEET_SCHEMAS:
            continue
        if sheet and sh != sheet:
            continue
        cols = coverage_columns(sh)
        raw = o.raw_fields or {}
        tickers = _row_tickers(o)
        cfn = cfcounts.get(o.ticker, 0)
        price = None
        if price_of is not None:
            # Precio de referencia: preferir la pata MEP (…D, en USD), luego CABLE (…C); la
            # pata pesos (…O) recién al final. Así las ONs muestran el precio en dólares MEP
            # (no el peso ~150990). Bonos peso-only (1 ticker ARS) no cambian.
            for t in sorted(tickers, key=lambda x: {"MEP": 0, "CABLE": 1}.get(ccy_from_suffix(x), 2)):
                p = price_of(t)
                if p:
                    price = p
                    break
        vals: Dict[str, Any] = {}
        filled = 0
        miss_req: List[str] = []
        miss_opt: List[str] = []
        sector_auto = False
        for c in cols:
            k = c["key"]
            v = (o.isin or raw.get("isin")) if k == "isin" else raw.get(k)
            # La Categoría (sector) de una ON se autocompleta del emisor cuando no hay
            # override manual → la columna muestra el sector efectivo de CADA ON (no "—").
            # Se muestra con la MISMA etiqueta corta que el monitor de ON (key→short);
            # un override legacy fuera del catálogo se deja tal cual. Auto = derivado.
            if k == "sector_override":
                key = v if (v and str(v).strip()) else None
                if not key:
                    key = classify_sector(o.short_name or "")
                    sector_auto = True
                meta = SECTOR_MAP.get(key)
                v = meta.short if meta else key
            vals[k] = v
            if v not in (None, "") and str(v).strip() != "":
                filled += 1
            elif c["required"]:
                miss_req.append(c["label"])
            else:
                miss_opt.append(c["label"])
        # cashflows: requerido, cuenta en el %
        total = len(cols) + 1
        if cfn > 0:
            filled += 1
        else:
            miss_req.append("Cashflows")
        pct = round(100 * filled / total) if total else 0
        health = "r" if miss_req else ("a" if miss_opt else "g")
        out.append({
            "sheet": sh, "key": o.ticker, "tickers": tickers,
            "display": " / ".join(tickers), "emisor": o.short_name or "",
            "vals": vals, "cf": cfn, "price": price, "has_price": bool(price),
            "pct": pct, "health": health, "missing": miss_req + miss_opt,
            "sector_auto": sector_auto,
        })
    out.sort(key=lambda e: (e["pct"], e["key"]))
    return out


def _type_field_for(sheet: str) -> Optional[str]:
    """Clave del form que lleva el `instrument_type` de esa hoja ('tipo'/'clase'),
    o None si la hoja no tiene campo de tipo (Dolar_Linked)."""
    keys = {f["key"] for f in SHEET_SCHEMAS.get(sheet, {}).get("fields", [])}
    for k in ("tipo", "clase"):
        if k in keys:
            return k
    return None


def audit_catalog_health() -> Dict[str, List[Dict[str, Any]]]:
    """Chequeo de salud de los tipos del catálogo, leyendo SQLite.

    Wrapper con I/O de `repositories.audit_catalog_types` (que es la lógica, sobre
    filas ya cargadas). Lo usa el operador a mano y lo puede consumir el ABM; el
    arranque NO pasa por acá — `CatalogRepository._load` audita las filas que ya
    tiene en la mano, sin una segunda query."""
    init_db()
    with SessionLocal() as s:
        from sqlalchemy.orm import noload
        rows = s.execute(
            select(InstrumentORM).options(noload(InstrumentORM.cashflows))
            .order_by(InstrumentORM.ticker)
        ).scalars().all()
        return audit_catalog_types(rows)


def audit_orphan_types() -> List[Dict[str, Any]]:
    """Bonos del catálogo cuyo `instrument_type` no pertenece a NINGÚN grupo de
    `core/domain/instrument_groups` → no se precian ni aparecen en ningún panel
    (los paneles filtran por igualdad exacta de tipo).

    Es el chequeo que faltaba: hoy un tipo huérfano entra en silencio (se carga,
    se guarda, tiene cashflows y precio) y nadie se entera. Lo consumen el script
    de migración `scripts/migrate_orphan_types.py`, la verificación post-migración
    y los tests. La señal de ARRANQUE va por `CatalogRepository.type_health`."""
    return audit_catalog_health()["orphans"]


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
    """Fila-bono que contiene `ticker_u` en cualquier slot (primario/mep/ccl).

    Igualdad directa sobre ticker_mep/ticker_ccl (index-backed por ix_instr_mep/ccl):
    los slots se guardan SIEMPRE en mayúsculas (_currency_tickers/split_currency_tickers
    normalizan), y `ticker_u` ya viene .upper().strip() del caller → preserva la
    semántica case-insensitive sin forzar un full scan (func.upper lo forzaría)."""
    orm = s.get(InstrumentORM, ticker_u)               # fast-path PK (primario)
    if orm is not None:
        return orm
    return s.execute(
        select(InstrumentORM).where(or_(InstrumentORM.ticker_mep == ticker_u,
                                        InstrumentORM.ticker_ccl == ticker_u))
    ).scalars().first()


def _byma_isin_for(s, tickers) -> Optional[str]:
    """ISIN del universo BYMA (byma_catalog) para cualquiera de las patas del bono."""
    from core.infrastructure.db.models import BymaCatalogORM
    ts = [t.upper() for t in tickers if t]
    if not ts:
        return None
    row = s.execute(
        select(BymaCatalogORM.isin)
        .where(BymaCatalogORM.symbol.in_(ts), BymaCatalogORM.isin.isnot(None))
    ).first()
    return row[0] if row else None


def get_instrument(ticker: str) -> Optional[Dict[str, Any]]:
    """{"sheet", "fields", "cashflows", "cashflows_source"} para CUALQUIER ticker
    del bono (primario o pata), o None. `fields` trae los slots ticker_ars/mep/ccl
    reconstruidos desde la fila (autoritativo).

    `cashflows_source`: "sheet" (los flujos REALES de la DB, sin el ancla) ·
    "analitico" (tipo de payoff cerrado: la tabla va vacía, ver abajo) · "synth"
    (propuesta del sintetizador para un bono sin flujos) · "empty"."""
    ticker_u = ticker.upper().strip()
    init_db()
    with SessionLocal() as s:
        orm = _find_bond_row(s, ticker_u)
        if orm is None:
            return None
        sheet = orm.sheet or ""
        fields = dict(orm.raw_fields or {})
        # Tipo: la COLUMNA manda cuando raw_fields no lo trae. Sin esto, las filas
        # sembradas por script (los ingest IAMC guardan raw_fields sin `tipo`) volvían
        # del round-trip get→save con el tipo recalculado del nombre de la hoja →
        # "OBLIGACIONES_NEGOCIABLES"/"SOBERANOS", invisibles en todos los paneles.
        tkey = _type_field_for(sheet)
        if tkey and not str(fields.get(tkey) or "").strip():
            fields[tkey] = orm.instrument_type or ""
        # los slots de ticker reflejan la fila (no los raw_fields, que pueden
        # estar viejos): clasificar cada ticker por sufijo.
        for k in ("ticker", *_SOB_SLOTS):
            fields.pop(k, None)
        for tk in _row_tickers(orm):
            fields[_sob_slot(tk)] = tk
        # ISIN: la columna manda (la setea el enrich BYMA / el último save). Si está
        # vacía, se busca en el universo BYMA (byma_catalog) por cualquiera de las patas
        # → el form lo muestra aunque el enrich del catálogo curado aún no lo haya fijado.
        fields["isin"] = orm.isin or _byma_isin_for(s, _row_tickers(orm)) or ""
        # El ANCLA no es un pago (ver `instrument_groups.ANALYTIC_PAYOFF_TYPES`): filtrarla
        # acá es el espejo de lo que hace `_orm_to_domain` para el motor. Sin esto salía
        # por la tabla EDITABLE del cajón como una fila `vto / 0.000000 / 0.000000`.
        # Incondicional —no sólo para los analíticos— por si un bono cambió de tipo y le
        # quedó el ancla vieja: sigue sin ser un flujo.
        cf_rows = [
            {
                "date": cf.fecha_pago.isoformat(),
                "amortization": float(cf.amortizacion),
                "interest": float(cf.cupon_interes),
            }
            for cf in orm.cashflows if not cf.es_ancla
        ]
        analitico = has_closed_form_payoff(orm.instrument_type)
    if analitico:
        # Payoff cerrado ⇒ la tabla del cajón va VACÍA, y **sin caer al synth**: lo que
        # el synth propusiera lo descarta `save_instrument` a propósito, así que ofrecerlo
        # sería ofrecerle al operador trabajo que se va a tirar. El form ya explica por
        # qué está vacía (`fragments/abm_form.html`).
        return {"sheet": sheet, "fields": fields, "cashflows": [],
                "cashflows_source": "analitico"}
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
    filas-por-pata pre-migración (para consolidarlas al guardar).

    `IN (...)` sobre cada columna indexada (PK + ix_instr_mep/ccl) → MULTI-INDEX OR,
    sin full scan + filtro Python. `ts` ya viene en mayúsculas (invariante de storage)."""
    ts = {str(t).upper().strip() for t in tickers if t}
    if not ts:
        return []
    return list(s.execute(
        select(InstrumentORM).where(or_(InstrumentORM.ticker.in_(ts),
                                        InstrumentORM.ticker_mep.in_(ts),
                                        InstrumentORM.ticker_ccl.in_(ts)))
    ).scalars().all())


def save_instrument(sheet: str, fields: Dict[str, Any],
                    cashflows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Alta/edición de un instrumento (1 fila por bono, hasta 3 tickers por moneda).

    Lee los tickers del form (ticker_ars/ticker_mep/ticker_ccl, o `ticker` único)
    → escribe UNA fila con primario + ticker_mep/ticker_ccl, consolidando cualquier
    fila-por-pata pre-existente. Todo en una transacción (fail-fast).

    WRITE-PATH DETERMINISTA (Fase 9). El schedule que se persiste sale de
    `cashflows` (lo que mostró el preview) o del bono que ya estaba; **acá NO se
    sintetiza**. Antes se llamaba `_safe_synth` al guardar y `cashflow_synth` lee el
    RELOJ para resolver el step-up del cupón (`_parse_coupon_rate(asof=...)`): el
    schedule que quedaba en la DB dependía del DÍA en que se hizo el alta — el mismo
    form daba 0,63% en 2026 y 1,18% en 2028. El synth queda como PREVIEW
    (`_synth_cashflows_for_fields`, que usan `get_instrument` y `/abm/preview_cashflows`).

    Dos reglas de cierre, por tipo:

    · **Payoff analítico** (PURO / DUAL / DUAL_CER_TAMAR, ver
      `instrument_groups.ANALYTIC_PAYOFF_TYPES`): se persiste SOLO la fila **ancla**
      (`es_ancla=1`, monto 0 al vencimiento). Un schedule nominal sería *incorrecto*
      para ellos —su pago sale de `tamar.tamar_dual_payoff_at`— y además llegaría al
      dominio (no es ancla) cambiando el pricing de esos 14 bonos. Exige vencimiento.
    · **Tipo normal sin flujos**: **rechazo**. Antes era un WARNING silencioso de
      `_safe_synth` que dejaba un bono IMPRICEABLE (cashflows=()) en la DB.
    """
    if sheet not in SHEET_SCHEMAS:
        raise ValueError(f"Unknown sheet '{sheet}'. Allowed: {list(SHEET_SCHEMAS)}")
    normalized = _normalize_fields(fields)
    tickers = _currency_tickers(normalized)
    if not tickers:
        raise ValueError("se requiere al menos un ticker (pesos / MEP / CABLE)")
    primary, mep, ccl = split_currency_tickers(tickers)
    all_tickers = [t for t in (primary, mep, ccl) if t]

    # Guard de tipo: un `instrument_type` fuera de instrument_groups deja al bono
    # invisible en TODOS los paneles (filtran por igualdad exacta) y sin pricing.
    # Se valida ANTES de abrir la transacción: fail-fast, sin escribir nada.
    #
    # `warn=False`: esto es un PRE-CHEQUEO, no el camino de escritura. Con el aviso
    # puesto, guardar una ON sin `tipo` logueaba el MISMO WARNING dos veces por click
    # (acá y de nuevo adentro de `build_instrument`, abajo) — un aviso repetido se lee
    # como dos filas afectadas y le baja el precio a la señal. La traza la deja el
    # camino real: si el save sigue, `build_instrument` avisa una vez; si el tipo es
    # huérfano, el ValueError de acá se lo dice al usuario en la cara (y el router lo
    # loguea), que es más fuerte que una línea de log.
    itype = _resolve_instrument_type(normalized, sheet, primary, warn=False)
    if not is_known_type(itype):
        raise ValueError(
            f"tipo '{itype}' no pertenece a ningún grupo de instrument_groups: el bono "
            f"no se preciaría ni aparecería en ningún panel. Elegí un tipo válido de "
            f"la hoja {sheet}.")

    parsed_cfs = _parse_cashflows(cashflows) if cashflows is not None else None
    analitico = has_closed_form_payoff(itype)
    if analitico and parsed_cfs:
        logger.warning(
            "ABM: %s es %s (payoff analítico) — se descartan los %d flujos del form y se "
            "guarda solo la fila ancla del vencimiento.", primary, itype, len(parsed_cfs))

    init_db()
    with SessionLocal.begin() as s:
        existing = _find_bond_rows(s, tickers + all_tickers)
        action = "updated" if existing else "created"

        if analitico:
            cfs: List[Cashflow] = []      # el schedule lo reemplaza la fila ancla (abajo)
        elif parsed_cfs is not None:
            cfs = [Cashflow(date=d, amortization=a, interest=i) for d, a, i in parsed_cfs]
        else:
            # Los flujos que ya tenía el bono (edición sin tocar el schedule). El ancla
            # NO se arrastra: si el tipo dejó de ser analítico, sería un pago fantasma.
            prev = next((o for o in existing if any(not cf.es_ancla for cf in o.cashflows)),
                        None)
            cfs = [] if prev is None else [
                Cashflow(date=cf.fecha_pago, amortization=cf.amortizacion,
                         interest=cf.cupon_interes)
                for cf in prev.cashflows if not cf.es_ancla
            ]

        # raw_fields: MERGE sobre el blob previo, no reemplazo. El form solo manda
        # las claves de SHEET_SCHEMAS[sheet]; asignar `normalized` entero borraba
        # todo lo demás — el cache `byma`/`ficha`, `origen`, `cupon_anual_pct` y el
        # `ley_aplicable` de las hojas que no tienen ese campo en el form. El form
        # SIGUE GANANDO sobre sus propias claves (incluido vaciarlas). Se acumulan
        # los blobs de todas las filas a consolidar, con el primario último (manda).
        prev_raw: Dict[str, Any] = {}
        for o in sorted(existing, key=lambda x: x.ticker == primary):
            prev_raw.update(o.raw_fields or {})

        for o in existing:
            s.delete(o)
        s.flush()  # libera las PK antes de re-insertar

        inst = build_instrument(normalized, sheet, cfs)  # ticker = primario
        if inst is None:
            raise ValueError("ticker is required")
        if analitico and inst.maturity_date is None:
            raise ValueError(
                f"{primary}: un {itype} necesita fecha de VENCIMIENTO. Su pago no sale de "
                f"un schedule sino de la fórmula cerrada TAMAR, así que en la DB se guarda "
                f"una sola fila ancla con esa fecha. Completá «Vencimiento» y guardá de nuevo.")
        if not analitico and not cfs:
            raise ValueError(
                f"{primary}: no se puede guardar un {itype} sin FLUJO DE FONDOS (quedaría "
                f"cargado pero impriceable: sin TIR, sin MD y sin V.Téc). El schedule ya no "
                f"se sintetiza al guardar —dependía del día del alta—: apretá «⟳ Previsualizar» "
                f"en el cajón para generarlo desde los datos del form (emisión, vencimiento, "
                f"cupón, frecuencia), revisá las filas —o cargalas a mano con «＋ fila» si el "
                f"schedule es irregular— y volvé a Guardar.")
        orm = instrument_to_orm(inst, sheet=sheet, raw_fields={**prev_raw, **normalized},
                                ticker_mep=mep, ticker_ccl=ccl)
        if analitico:
            # FILA ANCLA: el vencimiento, marcado. Existe para que el bono sea auditable
            # y visible en /cashflows; `_orm_to_domain` la filtra, así que el motor sigue
            # viendo `cashflows=()` — pricing bit-idéntico por construcción.
            orm.cashflows = [CashflowORM(ticker=inst.ticker, fecha_pago=inst.maturity_date,
                                         amortizacion=0.0, cupon_interes=0.0, es_ancla=True)]
        s.add(orm)

    logger.info("ABM: %s %s [%s] in %s%s", action, primary, ",".join(all_tickers), sheet,
                f" · {len(parsed_cfs)} cashflows" if parsed_cfs is not None else "")
    out: Dict[str, Any] = {"action": action, "ticker": primary, "sheet": sheet,
                           "tickers": all_tickers}
    if parsed_cfs is not None:
        # Lo PERSISTIDO, no lo recibido: en un tipo analítico las filas del form se
        # descartan y sobrevive sólo el ancla. Reportar `len(parsed_cfs)` ahí le decía
        # al caller (y al log del router) que guardó N flujos que no existen.
        out["cashflows"] = 0 if analitico else len(parsed_cfs)
        if analitico and parsed_cfs:
            out["descartados"] = len(parsed_cfs)
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
        # Misma regla que `save_instrument`: un tipo de payoff analítico no puede tener
        # schedule. Sin este guard, ésta era la puerta de atrás — las filas entrarían sin
        # marca de ancla, llegarían al dominio y le cambiarían el pricing a esos bonos.
        # Incondicional (no sólo con filas nuevas): pasar [] tampoco es válido — borraría
        # la fila ancla y devolvería el bono al estado invisible de antes de la Fase 9.
        if has_closed_form_payoff(orm.instrument_type):
            raise ValueError(
                f"{orm.ticker} es {orm.instrument_type}: su pago sale de una fórmula cerrada "
                f"(TAMAR observada + proyectada), no de un schedule. Cargarle flujos le "
                f"cambiaría la TIR y el V.Téc. En la DB lleva una sola fila ancla con el "
                f"vencimiento; para corregirla, editá «Vencimiento» en el form del bono.")
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


# Paneles multi-moneda donde tiene sentido completar patas D/C (tienen ccy-filter).
# NO se tocan CER/Tasa Fija/TAMAR/Dólar Linked: son instrumentos pesos de 1 ticker —
# sumarles D/C sería "fila basura" (ver memoria multi-ticker-model).
_MULTI_CCY_SHEETS = (_SOBERANOS_SHEET, "Obligaciones_Negociables")


def _universe_groups():
    """Grupos cotizantes del universo BYMA por ISIN y por ticker_pesos.
    Devuelve (by_isin, by_tp, by_sym): {clave: {moneda: symbol}} + índice symbol→fila.
    El ISIN es la clave AUTORITATIVA del activo (linkea bases distintas: BPC7↔BPOC7);
    ticker_pesos queda de fallback para grupos sin ISIN. Excluye no-cotizantes y .SB."""
    from core.infrastructure.db.models import BymaCatalogORM
    init_db()
    with SessionLocal() as s:
        uni = s.execute(select(BymaCatalogORM)).scalars().all()
    by_sym = {u.symbol.upper(): u for u in uni}
    by_isin: Dict[str, Dict[str, str]] = {}
    by_tp: Dict[str, Dict[str, str]] = {}
    for u in uni:
        if u.cotiza and not u.segmento:
            if u.isin:
                by_isin.setdefault(u.isin.upper(), {})[u.moneda] = u.symbol.upper()
            if u.ticker_pesos:
                by_tp.setdefault(u.ticker_pesos.upper(), {})[u.moneda] = u.symbol.upper()
    return by_isin, by_tp, by_sym


def backfill_legs_from_universe(dry_run: bool = False) -> List[Dict[str, Any]]:
    """Completa las patas COTIZANTES faltantes (pesos/MEP/CABLE) de soberanos y ONs
    deduciendo el activo por el universo BYMA. Agrupa por **ISIN** (clave del activo →
    linkea bases distintas como BPC7↔BPOC7); fallback a ticker_pesos. Reusa
    `save_instrument` (consolida en 1 fila, re-keya al primario, preserva cashflows).
    Idempotente. `dry_run` solo reporta. Devuelve [{ticker, added, key}]."""
    by_isin, by_tp, by_sym = _universe_groups()
    init_db()
    with SessionLocal() as s:
        rows = s.execute(
            select(InstrumentORM).where(InstrumentORM.sheet.in_(_MULTI_CCY_SHEETS))
        ).scalars().all()
        plan = []
        for o in rows:
            present = [t.upper() for t in _row_tickers(o)]
            present_set = set(present)
            avail: Dict[str, str] = {}
            keys = set()
            # 1) ticker_pesos de las patas presentes (fallback / mismo-base)
            for t in present:
                u = by_sym.get(t)
                if u and u.ticker_pesos and u.ticker_pesos.upper() in by_tp:
                    avail.update(by_tp[u.ticker_pesos.upper()])
                    keys.add(u.ticker_pesos.upper())
            # 2) ISIN (autoritativo, gana): el del bono curado + el de sus patas
            isins = {o.isin.upper()} if o.isin else set()
            isins |= {by_sym[t].isin.upper() for t in present
                      if t in by_sym and by_sym[t].isin}
            for isin in isins:
                if isin in by_isin:
                    avail.update(by_isin[isin])   # pisa al tp si difiere
                    keys.add(isin)
            missing = sorted({sym for sym in avail.values() if sym not in present_set})
            if missing:
                plan.append((o.ticker, o.sheet or "", present, missing, sorted(keys)))

    results: List[Dict[str, Any]] = []
    for primary, sheet, present, missing, keys in plan:
        results.append({"ticker": primary, "added": missing, "key": keys})
        if dry_run:
            continue
        inst = get_instrument(primary)
        if not inst:
            continue
        fields = dict(inst.get("fields", {}))
        for slot in _SOB_SLOTS:        # re-armar los slots con el set COMPLETO de patas
            fields.pop(slot, None)
        for tk in [*present, *missing]:
            fields[_sob_slot(tk)] = tk
        try:
            save_instrument(sheet, fields)   # cashflows=None → reusa los existentes
        except (ValueError, KeyError) as e:
            logger.warning("backfill_legs %s falló: %s", primary, e)
            results[-1]["error"] = str(e)
    if not dry_run and results:
        logger.info("Backfill patas universo: %d bonos completados (+%d patas).",
                    len(results), sum(len(r["added"]) for r in results))
    return results
