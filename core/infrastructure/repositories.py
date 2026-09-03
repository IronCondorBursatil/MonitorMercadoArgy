import logging
import warnings

import pandas as pd
from typing import Any, List, Dict, Optional
from datetime import date, datetime
from core.domain.currency import ccy_from_suffix
from core.domain.instrument_groups import is_known_type, orphan_types
from core.domain.models import Instrument, Cashflow
from core.domain.interfaces import IInstrumentsRepository
# Re-export por compatibilidad: el provider de market-data se movió a su propio
# módulo (M2.3) pero algunos consumidores aún lo importan desde acá.
from core.infrastructure.data912_provider import Data912MarketDataProvider  # noqa: F401

# Silence unnecessary pandas warnings for cleaner console
warnings.filterwarnings("ignore", category=UserWarning, module="pandas")

logger = logging.getLogger(__name__)


def _infer_payment_frequency(cashflows) -> int:
    """Best-guess annual payment frequency from observed cashflow dates.
    Single-flow bonds → 1 (degenerate case; MD bullet formula applies).
    Multi-flow: median gap in months → 12/gap, rounded to common frequencies."""
    if not cashflows or len(cashflows) < 2:
        return 1
    dates = sorted(cf.date for cf in cashflows)
    gaps_days = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    if not gaps_days:
        return 1
    median_gap = sorted(gaps_days)[len(gaps_days) // 2]
    if median_gap <= 0:
        return 2
    freq = round(365.0 / median_gap)
    # Snap to standard market frequencies.
    for std in (12, 4, 2, 1):
        if abs(freq - std) <= 1:
            return std
    return max(1, min(12, freq))


# --------------------------------------------------------------------------- #
# Parsing de fila → Instrument (compartido por el loader Excel y el ABM SQLite).
# Acepta cualquier mapping con `.get`/`in`: una pandas Series (loader) o un dict
# (form del ABM, claves ya en minúscula).
# --------------------------------------------------------------------------- #

def _isna(v) -> bool:
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _safe_int(val, default: int = 0) -> int:
    try:
        if pd.isna(val):
            return default
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _safe_float(val, default: float = 0.0) -> float:
    try:
        if pd.isna(val):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _opt_float(val, field: str = "", ticker: str = "") -> Optional[float]:
    """Float opcional: None si la celda está vacía/ausente, y None + WARNING si trae
    algo no numérico ('n/d', 's/d', un guión). Antes estos tres campos
    (`tasa_fija_mensual`/`tem_licit`, `spread`, `cer_spread`) usaban `float()` pelado
    —a diferencia de sus vecinos `cer_base`/`cer_lag`, que ya iban por `_safe_float`—
    y una sola celda con texto en el master abortaba la carga de la hoja entera."""
    if val is None or _isna(val):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        logger.warning("celda no numérica en '%s' de %s: %r — se ignora el campo.",
                       field, ticker or "?", val)
        return None


def _parse_date_value(val) -> Optional[date]:
    """Parsea date de varios formatos sin warnings.

    Crítico: detectar ISO (YYYY-MM-DD) y parsear con dayfirst=False. pandas con
    dayfirst=True swapea mes/día en strings ISO (corrompía el schedule de cupones
    de los soberanos, que guardan fechas ISO en la hoja Cashflows)."""
    if val is None or _isna(val):
        return None
    if isinstance(val, pd.Timestamp):
        return val.date()
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    s = str(val).strip()
    if not s:
        return None
    iso_like = (len(s) >= 10 and s[4] == "-" and s[7] == "-" and s[:4].isdigit())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            dt = pd.to_datetime(s, dayfirst=not iso_like, errors="coerce")
            return dt.date() if pd.notna(dt) else None
        except (ValueError, TypeError):
            return None


def _resolve_ticker(row) -> Optional[str]:
    raw = None
    for cand in ("ticker", "ticker_ref", "symbol"):
        if cand in row:
            raw = str(row.get(cand)).upper().strip()
            break
    if not raw or raw in ("NAN", "NONE"):
        return None
    return raw


# Multi-ticker: un instrumento puede cotizar en hasta 3 monedas con tickers
# distintos (mismo bono, mismos flujos): pesos (ARS) / MEP (sufijo D) / CABLE (C).
_CCY_TICKER_COLS = ("ticker_ars", "ticker_mep", "ticker_ccl")


def _currency_tickers(row) -> List[str]:
    """Tickers de un instrumento (1-3), en orden de slot de moneda. Soporta el
    esquema nuevo (ticker_ars/ticker_mep/ticker_ccl) y el viejo (`ticker` único).
    El primero es el primario (PK / clave de cashflows)."""
    out: List[str] = []
    for col in _CCY_TICKER_COLS:
        if col in row:
            v = row.get(col)
            if v is not None and not _isna(v):
                t = str(v).upper().strip()
                if t and t not in ("NAN", "NONE") and t not in out:
                    out.append(t)
    if out:
        return out
    t = _resolve_ticker(row)
    return [t] if t else []


def expand_currency_legs(primary: Instrument, secondary_tickers) -> List[Instrument]:
    """Una especie (Instrument) por ticker: primario + secundarios, compartiendo
    términos y flujos. Cada especie linkea con su precio de Data912 por su propio
    ticker; la moneda la deriva el motor del sufijo (D=MEP, C=CABLE)."""
    legs = [primary]
    seen = {primary.ticker}
    for t in secondary_tickers or ():
        tu = str(t).upper().strip()
        if tu and tu not in seen:
            legs.append(primary.model_copy(update={"ticker": tu}))
            seen.add(tu)
    return legs


def split_currency_tickers(tickers: List[str]):
    """[primario, *secundarios] → (primario, ticker_mep, ticker_ccl) para storage.
    Los secundarios se asignan por sufijo (D→mep, C→ccl); sin sufijo van a la
    primera ranura libre. Solo para que las columnas queden semánticas; la
    expansión y el pricing usan el sufijo, no la columna."""
    primary = tickers[0]
    mep = ccl = None
    for t in tickers[1:]:
        tu = str(t).upper().strip()
        if not tu:
            continue
        ccy = ccy_from_suffix(tu)
        if ccy == "MEP" and not mep:
            mep = tu
        elif ccy == "CABLE" and not ccl:
            ccl = tu
        elif not mep:
            mep = tu
        elif not ccl:
            ccl = tu
    return primary, mep, ccl


def _first_present(row, keys):
    """Valor de la primera clave que EXISTE (aunque su valor sea None/NaN) —
    replica la semántica de `row.get(a, row.get(b, ...))`."""
    for k in keys:
        if k in row:
            return row.get(k)
    return None


def _first_date(row, candidates) -> Optional[date]:
    for c in candidates:
        if c in row:
            d = _parse_date_value(row.get(c))
            if d:
                return d
    return None


# Tipo canónico por hoja para las filas que NO traen `tipo`/`clase`. Antes el
# fallback era el NOMBRE DE LA HOJA ("Obligaciones_Negociables" → tipo
# "OBLIGACIONES_NEGOCIABLES"), que no pertenece a ningún grupo de
# `instrument_groups` → el bono se carga pero NINGÚN panel lo muestra ni lo precia
# (los paneles filtran por igualdad exacta). Solo se mapean las hojas con un
# default inequívoco; las ambiguas (Soberanos = BONAR/GLOBAL/BOPREAL) exigen el
# campo explícito y avisan por log si falta.
_SHEET_DEFAULT_TYPE = {
    "OBLIGACIONES_NEGOCIABLES": "HARD DOLLAR",   # on_catalog.ITYPE (las DL se cargan a mano)
    "DOLAR_LINKED": "DOLAR_LINKED",              # la hoja DL no tiene columna `tipo`
    "ACCIONES": "ACCION",
}

# Hojas cuyo default de arriba es una SUPOSICIÓN, no una verdad: la hoja admite
# más de un `instrument_type` y elegir mal cambia la MONEDA DE PAGO.
#
#   Obligaciones_Negociables → HARD DOLLAR (paga USD) | DOLLAR LINKED (paga
#   pesos × FX). Una ON dollar-linked que entre sin `tipo` NO queda invisible:
#   queda VISIBLE y preciada con la strategy equivocada, que es peor (un número
#   creíble y mal). Por eso el default deja traza: WARNING por fila en el borde
#   de escritura (abajo) + inventario por catálogo en `audit_catalog_types`.
#
# Las otras dos hojas del mapa NO son ambiguas: `Dolar_Linked` tiene un único
# tipo posible (la hoja ni siquiera lleva columna `tipo`) y `Acciones` idem.
AMBIGUOUS_DEFAULT_SHEETS = frozenset({"OBLIGACIONES_NEGOCIABLES"})

# Claves del form/fila que pueden llevar el tipo explícito, en orden de prioridad.
_TYPE_KEYS = ("tipo", "clase")


def explicit_type_of(row) -> str:
    """El `instrument_type` DECLARADO por la fila (`tipo`/`clase`), o '' si no lo
    trae. Un '' acá significa que el tipo, si lo hay, salió de un default de hoja
    — no del dato."""
    for key in _TYPE_KEYS:
        v = row.get(key) if key in row else None
        if v is not None and not _isna(v) and str(v).strip():
            return str(v).upper().strip()
    return ""


def _resolve_instrument_type(row, sheet: str, ticker: str, *, warn: bool = True) -> str:
    """`instrument_type` de una fila: `tipo`/`clase` explícito > default de la hoja.

    NUNCA devuelve el nombre crudo de la hoja sin avisar: si el resultado no
    pertenece a ningún grupo de `instrument_groups`, loguea WARNING con el ticker
    (el bono quedaría invisible en todos los paneles). Y si el tipo salió de un
    default AMBIGUO (ver `AMBIGUOUS_DEFAULT_SHEETS`), también avisa: ahí el riesgo
    no es la invisibilidad sino un pricing en la moneda equivocada.

    `warn=False` resuelve el tipo EN SILENCIO: es para los PRE-CHEQUEOS que corren
    antes del camino real (el guard de `instruments_abm.save_instrument`, que valida
    y después llama a `build_instrument`). Sin esa perilla, un save de ON sin `tipo`
    dejaba el MISMO WARNING dos veces por click —una por el guard, otra por el
    build— y un aviso duplicado se lee como dos filas afectadas."""
    itype = explicit_type_of(row)
    if not itype:
        sheet_key = str(sheet or "").upper().strip()
        itype = _SHEET_DEFAULT_TYPE.get(sheet_key, sheet_key)
        if warn and sheet_key in AMBIGUOUS_DEFAULT_SHEETS:
            logger.warning(
                "instrument_type ASUMIDO '%s' (ticker %s, hoja %s): la fila no declara "
                "`tipo` y la hoja admite más de uno. Si el bono es dollar-linked se va a "
                "preciar como hard-dollar (moneda de pago equivocada) — cargá el tipo "
                "explícito por ABM.", itype, ticker, sheet)
    if warn and itype and not is_known_type(itype):
        logger.warning(
            "instrument_type '%s' (ticker %s, hoja %s) no pertenece a ningún grupo de "
            "instrument_groups: el bono NO se va a preciar ni a ver en ningún panel.",
            itype, ticker, sheet)
    return itype


def _health_entry(orm) -> Dict[str, Any]:
    """Fila del reporte de salud (misma forma que consume el ABM y el script de
    migración): primario + patas de moneda + hoja + tipo."""
    tickers = [t for t in (getattr(orm, "ticker", None), getattr(orm, "ticker_mep", None),
                           getattr(orm, "ticker_ccl", None)) if t]
    return {"ticker": getattr(orm, "ticker", "") or "", "tickers": tickers,
            "sheet": getattr(orm, "sheet", "") or "",
            "short_name": getattr(orm, "short_name", "") or "",
            "instrument_type": getattr(orm, "instrument_type", "") or ""}


def _would_lose_type(entry: Dict[str, Any]) -> bool:
    """¿Esta fila `defaulted` PERDERÍA su tipo si se la reconstruyera desde
    `raw_fields` (round-trip del ABM, un ingest)?

    Sólo si HOY tiene un tipo VÁLIDO distinto del default de su hoja: ahí la
    reconstrucción le cambia la strategy de pricing (una ON dollar-linked pasaría a
    preciarse como hard-dollar — otra moneda de pago).

    Una fila HUÉRFANA no entra: su tipo actual ya no lo entiende ningún panel, así
    que reconstruirla no le hace perder nada (le daría uno válido, la arreglaría).
    Contarlas acá DOBLE-CONTABA el balde `orphans` —el mismo bono inflando dos
    números del mismo WARNING— y, peor, el orden las empujaba al tope del reporte,
    tapando justo a las que sí corren el riesgo que el mensaje describe."""
    return (is_known_type(entry.get("instrument_type"))
            and entry.get("default_type") != entry.get("instrument_type"))


def audit_catalog_types(rows, *, log: bool = True) -> Dict[str, List[Dict[str, Any]]]:
    """Señal de salud de los `instrument_type` del catálogo, sobre filas-bono ORM.

    Devuelve dos baldes (cada uno con la forma de `_health_entry`):

      · ``orphans``   — tipo que no pertenece a NINGÚN grupo de `instrument_groups`.
        El bono se carga, guarda cashflows y acumula precio, pero **ningún panel lo
        muestra ni lo precia** (todo el read-path filtra por igualdad exacta).
      · ``defaulted`` — tipo NO declarado en `raw_fields` sobre una hoja de default
        ambiguo (`AMBIGUOUS_DEFAULT_SHEETS`). El bono SÍ se ve, pero su tipo es una
        suposición: una ON dollar-linked ahí queda preciada como hard-dollar.

    `log=True` deja constancia por WARNING (el archivo de log persiste WARNING+, así
    que la señal sobrevive al post-mortem). Es el chequeo de salud que faltaba: hasta
    ahora un tipo huérfano entraba en silencio y nadie se enteraba."""
    orphans = [_health_entry(o) for o in rows if not is_known_type(getattr(o, "instrument_type", ""))]
    defaulted = []
    for o in rows:
        sheet_key = str(getattr(o, "sheet", "") or "").upper().strip()
        if sheet_key not in AMBIGUOUS_DEFAULT_SHEETS:
            continue
        if explicit_type_of(getattr(o, "raw_fields", None) or {}):
            continue
        e = _health_entry(o)
        # Lo que saldría si alguien reconstruyera la fila desde el blob (el
        # round-trip del ABM, un ingest). Si difiere del tipo actual Y el actual es
        # válido, la fila PERDERÍA su tipo en esa reconstrucción (`_would_lose_type`)
        # → esas van primero en el reporte.
        e["default_type"] = _SHEET_DEFAULT_TYPE.get(sheet_key, sheet_key)
        defaulted.append(e)
    defaulted.sort(key=lambda e: (not _would_lose_type(e), e["ticker"]))
    if log and orphans:
        logger.warning(
            "catálogo: %d bono(s) con instrument_type huérfano (%s) — invisibles en "
            "todos los paneles: %s", len(orphans),
            ", ".join(orphan_types(e["instrument_type"] for e in orphans)),
            " ".join(e["ticker"] for e in orphans[:20]))
    if log and defaulted:
        divergen = [e for e in defaulted if _would_lose_type(e)]
        huerfanas = sum(1 for e in defaulted if not is_known_type(e["instrument_type"]))
        logger.warning(
            "catálogo: %d bono(s) sin `tipo` declarado en hoja de default ambiguo — el "
            "tipo mostrado es una SUPOSICIÓN (una ON dollar-linked ahí se precia como "
            "hard-dollar); %d perderían su tipo si se los reconstruyera desde "
            "raw_fields%s: %s", len(defaulted), len(divergen),
            f" ({huerfanas} ya cuenta(n) como huérfano)" if huerfanas else "",
            " ".join(e["ticker"] for e in defaulted[:20]))
    return {"orphans": orphans, "defaulted": defaulted}


def build_instrument(row, sheet: str, cashflows: List[Cashflow]) -> Optional[Instrument]:
    """Construye el `Instrument` PRIMARIO desde una fila (mapping de columnas en
    minúscula) + hoja + cashflows. Devuelve None si no hay ticker. Las patas de
    moneda secundarias se expanden aparte (`expand_currency_legs`)."""
    tickers = _currency_tickers(row)
    if not tickers:
        return None
    raw_ticker = tickers[0]
    short = str(row.get("short_name", row.get("short name", raw_ticker)))
    itype = _resolve_instrument_type(row, sheet, raw_ticker)
    m_date = _first_date(row, ("fecha_vencimiento", "fecha vencimiento", "fecha_pago", "maturity"))
    e_date = _first_date(row, ("fecha_emision", "fecha emision"))

    # cer base: "cer emision" (legacy) / "cer_emision" (snake) / "cer_base" (TAMAR duals).
    cer_b = _safe_float(_first_present(row, ("cer emision", "cer_emision", "cer_base")), default=1.0)
    lag_val = _safe_int(_first_present(row, ("dias habiles previos", "dias_lag")), default=10)

    cat_raw = row.get("categoria")
    category = str(cat_raw).strip() if (cat_raw is not None and not _isna(cat_raw)) else None
    if category is None and itype in ("HARD DOLLAR", "DOLLAR LINKED"):  # categoría fija de las ONs
        category = "Obligaciones Negociables"

    floor_raw = row.get("tasa_fija_mensual") or row.get("tem_licit")
    floor = _opt_float(floor_raw, "tasa_fija_mensual/tem_licit", raw_ticker)

    spread_raw = _first_present(row, ("spread", "spread_anual"))
    spread = _opt_float(spread_raw, "spread", raw_ticker)

    cer_spread_raw = _first_present(row, ("cer_spread", "spread_cer"))
    cer_spread_val = _opt_float(cer_spread_raw, "cer_spread", raw_ticker)

    freq_raw = _first_present(row, ("frecuencia pagos", "frecuencia"))
    freq = _safe_int(freq_raw, default=0) if freq_raw is not None else 0
    if freq <= 0:
        freq = _infer_payment_frequency(cashflows)

    dc_raw = _first_present(row, ("base calculo", "base_calculo"))
    dc_str = str(dc_raw).strip() if (dc_raw is not None and not _isna(dc_raw)) else ""
    if not dc_str and itype == "BOPREAL":          # 30/360 por prospecto BCRA
        dc_str = "30/360"
    elif not dc_str and itype in ("HARD DOLLAR", "DOLLAR LINKED"):  # ON: real/365 (informe)
        dc_str = "ACT/365"
    day_count = dc_str or "ACT/365.25"

    ley_raw = _first_present(row, ("ley_aplicable", "ley aplicable", "ley"))
    ley = str(ley_raw).strip() if (ley_raw is not None and not _isna(ley_raw)) else None

    isin_raw = _first_present(row, ("isin", "codigoisin", "codigo_isin"))
    isin = str(isin_raw).strip().upper() if (isin_raw is not None and not _isna(isin_raw)) else None

    return Instrument(
        ticker=raw_ticker, short_name=short, instrument_type=itype,
        maturity_date=m_date, emission_date=e_date, cashflows=cashflows,
        cer_base=cer_b, cer_lag=lag_val, category=category,
        floor_rate_monthly=floor, spread_rate=spread, cer_spread=cer_spread_val,
        payment_frequency=freq, day_count=day_count, ley_aplicable=ley or None,
        isin=isin or None,
    )


def _json_scalar(v):
    """Coerce un valor de celda a un escalar JSON-serializable (para raw_fields)."""
    if v is None or _isna(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if hasattr(v, "item"):  # escalares numpy
        v = v.item()
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def row_to_raw_fields(row) -> Dict[str, object]:
    """Serializa una fila (Series/dict, claves en minúscula) a un dict JSON-safe."""
    items = row.items() if hasattr(row, "items") else []
    return {str(k).lower().strip(): _json_scalar(v) for k, v in items}


class ExcelInstrumentsRepository(IInstrumentsRepository):
    NON_INSTRUMENT_SHEETS = frozenset({"Cashflows", "Cashflows_Fija", "Metadata", "Cotizaciones"})

    def __init__(self, excel_path: str):
        self.excel_path = excel_path
        self._cache_instruments: List[Instrument] = []
        self._by_ticker: Dict[str, Instrument] = {}
        self._by_type: Dict[str, List[Instrument]] = {}
        # ticker -> (sheet, raw_fields) para el seeding del ABM (round-trip del form).
        self._meta: Dict[str, tuple] = {}
        self._load_all()

    # Delegaciones a los helpers módulo (compartidos con build_instrument / ABM).
    def _parse_date(self, val) -> Optional[date]:
        return _parse_date_value(val)

    def _generate_bond_cashflows(self, row: pd.Series) -> List[Cashflow]:
        """Delegate al synth puro en core.domain.cashflow_synth.

        Antes este método tenía ~130 LOC con dispatch por tipo + lógica de
        amortizing/bullet/zero-coupon/step-up. Extraído a un módulo puro
        para que la ABM también lo use (vía `synth_cashflows`) sin tener que
        instanciar Repository.__new__.
        """
        from core.domain.cashflow_synth import synth_cashflows
        return synth_cashflows(row)

    def _load_cashflow_sheet(
        self,
        sheet_name: str,
        amort_col: str,
        interest_col: Optional[str],
        cf_map: Dict[str, List[Cashflow]],
        required: bool = True,
    ) -> int:
        """Append Cashflow rows from `sheet_name` to `cf_map`. Returns rows skipped."""
        try:
            df = pd.read_excel(self.excel_path, sheet_name=sheet_name)
        except (FileNotFoundError, ValueError, KeyError) as e:
            if required:
                raise
            logger.debug(f"{sheet_name} sheet not loaded: {e}")
            return 0

        skipped = 0
        for _, row in df.iterrows():
            t = str(row.get("ticker", "")).upper().strip()
            if not t or t == "NAN":
                continue
            cf_date = self._parse_date(row.get("fecha_pago"))
            if cf_date is None:
                skipped += 1
                continue
            cf_map.setdefault(t, []).append(Cashflow(
                date=cf_date,
                amortization=_safe_float(row.get(amort_col, 0)),
                interest=_safe_float(row.get(interest_col, 0)) if interest_col else 0.0,
            ))
        return skipped

    def _load_all(self):
        # El Excel ya es solo semilla: la ABM escribe SQLite, no toca el master.
        # Por eso ya no hace falta compartir lock con instruments_abm.
        return self._load_all_impl()

    def _load_all_impl(self):
        try:
            # Rows with no parseable date are skipped to avoid TypeError when
            # comparing cf.date >= reference downstream.
            cf_map: Dict[str, List[Cashflow]] = {}
            skipped = self._load_cashflow_sheet("Cashflows", "amortizacion", "cupon_interes", cf_map)
            skipped += self._load_cashflow_sheet("Cashflows_Fija", "monto", None, cf_map, required=False)

            if skipped:
                logger.warning(f"Skipped {skipped} cashflow rows with invalid fecha_pago.")

            # bonos[primary_ticker] = (primary_inst, sheet, raw_fields, secondary_tickers)
            # Una entrada por BONO (no por pata de moneda); hojas posteriores pisan.
            bonos: Dict[str, tuple] = {}
            xl = pd.ExcelFile(self.excel_path)
            sheet_names = [s for s in xl.sheet_names if s not in self.NON_INSTRUMENT_SHEETS]

            for sheet in sheet_names:
                try:
                    df = xl.parse(sheet)
                    df.columns = [str(c).lower().strip() for c in df.columns]
                except Exception as e:
                    logger.warning(f"Could not load sheet {sheet}: {e}")
                    continue

                # El try va POR FILA: antes envolvía el `for` entero, así que una
                # sola celda mala descartaba en silencio TODAS las filas siguientes
                # de la hoja (los paneles quedaban con menos bonos, sin más señal
                # que un WARNING sin ticker).
                for _, row in df.iterrows():
                    tickers = _currency_tickers(row)
                    if not tickers:
                        continue
                    primary, secondaries = tickers[0], tickers[1:]
                    try:
                        short = str(row.get("short_name", row.get("short name", primary)))
                        m_date = _first_date(row, ("fecha_vencimiento", "fecha vencimiento",
                                                   "fecha_pago", "maturity"))

                        # Cashflows (hoja Cashflows por short/primario) o synth fallback.
                        cfs = cf_map.get(short.upper(), cf_map.get(primary, []))
                        if not cfs and m_date:
                            cfs = self._generate_bond_cashflows(row)

                        inst = build_instrument(row, sheet, cfs)
                    except Exception as e:
                        logger.warning("Fila %s de la hoja %s descartada: %s",
                                       primary, sheet, e)
                        continue
                    if inst is None:
                        continue
                    bonos[primary] = (inst, sheet, row_to_raw_fields(row), secondaries)

            # Expandir cada bono a una especie por ticker (pricing/paneles las ven
            # como instrumentos independientes, c/u con su precio de Data912).
            self._bonos = list(bonos.values())
            self._cache_instruments = []
            self._meta = {}
            for inst, sheet, raw, secondaries in self._bonos:
                for leg in expand_currency_legs(inst, secondaries):
                    self._cache_instruments.append(leg)
                    self._meta[leg.ticker] = (sheet, raw)

            self._by_ticker = {i.ticker: i for i in self._cache_instruments}
            by_type: Dict[str, List[Instrument]] = {}
            for inst in self._cache_instruments:
                by_type.setdefault(inst.instrument_type, []).append(inst)
            self._by_type = by_type

            logger.info(f"Repository loaded {len(self._cache_instruments)} unique instruments "
                        f"({len(self._bonos)} bonos).")
        except Exception as e:
            # Se recuerda el fallo: `get_all_with_meta` (el camino de SIEMBRA) lo
            # convierte en excepción para no sembrar un catálogo vacío en silencio.
            self._load_error = e
            logger.error(f"Error loading Excel repository: {e}")

    def get_all_instruments(self) -> List[Instrument]:
        return self._cache_instruments

    def get_instruments_by_type(self, instrument_type: str) -> List[Instrument]:
        return list(self._by_type.get(instrument_type, ()))

    def get_instrument_by_ticker(self, ticker: str) -> Optional[Instrument]:
        return self._by_ticker.get(ticker)

    def get_all_with_meta(self):
        """[(primary_inst, sheet, raw_fields, secondary_tickers), ...] — UNA entrada
        por bono (no por pata) para el seeding 1-fila-por-instrumento de SQLite.

        LANZA si la carga del Excel falló o no aportó ningún instrumento: este es el
        camino de SIEMBRA (`ingest_from_excel`), y devolver `[]` sembraba un catálogo
        vacío sin que nada abortara — el guard anti-pérdida de `reseed_with_meta` no
        puede disparar con la DB vacía (bootstrap de un droplet nuevo) ni con
        `--force`. Los lectores (`get_all_instruments`) siguen degradando suave."""
        err = getattr(self, "_load_error", None)
        bonos = list(getattr(self, "_bonos", []))
        if err is not None or not bonos:
            raise RuntimeError(
                f"el Excel semilla {self.excel_path} no aportó ningún instrumento"
                + (f": {err}" if err is not None else " (0 filas parseables)"))
        return bonos
