"""Derivaciones puras sobre los registros parseados de CAFCI: normalización,
subcategoría estilo fonditos, bucket de categoría, join de AUM (ArgentinaDatos por
nombre) y unificación de clases (1 registro por fondo con `clases[]`).

Portado de `scripts/build_fci_mock_data.py` (borrado). Sin red ni disco → testeable.
"""
from __future__ import annotations

import unicodedata
from typing import Dict, List, Optional

from core.domain.fci import PERIODS


def norm(s) -> str:
    """Lowercase + sin acentos para match/keywords robustos."""
    if not s:
        return ""
    t = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def seed(*parts) -> float:
    """[0,1) pseudo-aleatorio determinístico de un string (sin Math.random/Date)."""
    h = 2166136261
    for p in parts:
        for ch in str(p):
            h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return (h % 100000) / 100000.0


DURATION_YEARS = {
    "menor o igual a 0.5 ano": 0.5,
    "menor o igual a 1 ano": 1.0,
    "mayor a 1 y menor igual 3 anos": 3.0,
    "mayor a 3 anos": 5.0,
}


def dur_years(duration_name) -> Optional[float]:
    return DURATION_YEARS.get(norm(duration_name))


def ccy(moneda_name) -> str:
    n = norm(moneda_name)
    if "dolar" in n or "usd" in n or "estadounidense" in n:
        return "USD"
    return "ARS"


def settle(dl) -> str:
    if dl is None:
        return "—"
    if dl >= 99999:
        return "Cerrado"
    return f"T+{int(dl)}"


_CAT_BUCKET = {
    "Mercado de Dinero": "Mercado de Dinero",
    "Renta Fija": "Renta Fija",
    "Renta Variable": "Renta Variable",
    "Renta Mixta": "Renta Mixta",
    "Retorno Total": "Retorno Total",
}


def category_bucket(tr) -> str:
    return _CAT_BUCKET.get(tr, "Otros")


def subcategoria(tr, moneda_ccy, tipo_dinero, dur, dl, nombre, obj, region) -> str:
    """Subcategoría estilo fonditos, derivada de campos CAFCI + keywords en nombre/objetivo."""
    name = norm(nombre) + " " + norm(obj)
    if tr == "Mercado de Dinero":
        if moneda_ccy == "USD":
            return "MONEY MARKET USD"
        return "MONEY MARKET ARS DINÁMICO" if norm(tipo_dinero) == "dinamico" else "MONEY MARKET ARS CLÁSICO"
    if tr == "Renta Fija":
        if any(k in name for k in ("dolar link", "dollar link", "dolar-link", " dl ", "linked")):
            return "RENTA FIJA DÓLAR LINKED"
        if any(k in name for k in ("cer", " uva", "inflacion", "ajustable")):
            return "RENTA FIJA CER"
        if any(k in name for k in ("lecap", "lecer", "tasa fija", "boncap")):
            return "RENTA FIJA LECAP / TASA FIJA"
        if moneda_ccy == "USD":
            if dur is not None and dur <= 1:
                return "RENTA FIJA USD CORTO PLAZO"
            return "RENTA FIJA USD"
        if (dl is not None and dl <= 1) and (dur is not None and dur <= 0.5):
            return "RENTA FIJA T+1"
        return "RENTA FIJA ARS DISCRECIONAL"
    if tr == "Renta Mixta":
        return f"RENTA MIXTA {moneda_ccy}"
    if tr == "Renta Variable":
        rn = norm(region)
        if rn and rn != "argentina":
            return "RENTA VARIABLE GLOBAL / LATAM"
        return "RENTA VARIABLE ARGENTINA"
    if tr == "Retorno Total":
        return "RETORNO TOTAL"
    return (tr or "OTROS").upper()


# --------------------------------------------------------------------------- #
# AUM join (ArgentinaDatos por nombre) — best-effort exacto → base → prefijo.
# --------------------------------------------------------------------------- #
def build_aum_index(ard_rows: List[dict]) -> Dict[str, dict]:
    """`{nombre_normalizado: {aum, ccp}}` desde filas ArgentinaDatos {fondo,patrimonio,ccp}."""
    idx: Dict[str, dict] = {}
    for r in ard_rows or ():
        k = norm(r.get("fondo"))
        aum = to_float(r.get("patrimonio"))
        if k and aum:
            idx[k] = {"aum": aum, "ccp": to_float(r.get("ccp"))}
    return idx


def lookup_aum(aum_index: Dict[str, dict], fondo_nombre, clase_nombre):
    """Match por nombre de clase exacto → 'fondo - clase' → base 'fondo' → prefijo.
    El nombre de clase de CAFCI suele ser el nombre COMPLETO (= el `fondo` de ArgentinaDatos),
    así que probarlo primero da AUM preciso por clase. Devuelve ({aum,ccp}, real)."""
    ck = norm(clase_nombre)
    if ck and ck in aum_index:
        return aum_index[ck], True
    full = norm(f"{fondo_nombre} - {clase_nombre}")
    if full in aum_index:
        return aum_index[full], True
    base = norm(fondo_nombre)
    if base in aum_index:
        return aum_index[base], True
    if base:
        for k, v in aum_index.items():
            if k.startswith(base):
                return v, True
    return None, False


# --------------------------------------------------------------------------- #
# Unificación: parsed records (por clase) → 1 registro por fondo con `clases[]`.
# --------------------------------------------------------------------------- #
def _rend(rec) -> dict:
    return {p: {"tna": (rec.get("rend", {}).get(p) or {}).get("tna"),
                "directo": (rec.get("rend", {}).get(p) or {}).get("directo")} for p in PERIODS}


def unify_classes(parsed_funds: List[dict], aum_index: Dict[str, dict]) -> List[dict]:
    """Agrupa los registros por fondo. Cada fondo = campos compartidos + `clases[]`
    (clase principal = mayor AUM). AUM no matcheado → None (honesto, sin inventar).
    `aum` del fondo = suma de AUMs únicos de sus clases (evita ×N por el join difuso)."""
    by_fondo: Dict[object, List[dict]] = {}
    order: List[object] = []
    for rec in parsed_funds:
        fid = rec.get("fondo_id")
        if fid not in by_fondo:
            by_fondo[fid] = []
            order.append(fid)
        by_fondo[fid].append(rec)

    out: List[dict] = []
    for fid in order:
        recs = by_fondo[fid]
        clases = []
        for rec in recs:
            aum, aum_real = lookup_aum(aum_index, rec.get("fondo_nombre"), rec.get("clase_nombre"))
            clases.append({
                "cid": rec.get("clase_id"), "clase": rec.get("clase_nombre"),
                "moneda": ccy(rec.get("moneda")), "moneda_full": rec.get("moneda"),
                "vcp": rec.get("vcp"), "fecha": rec.get("fecha_valor"), "rend": _rend(rec),
                "fee_admin": rec.get("fee_admin"), "fee_in": rec.get("fee_in"),
                "fee_out": rec.get("fee_out"), "min": rec.get("inversion_minima"),
                "isin": rec.get("ticker_isin"), "bbg": rec.get("ticker_bloomberg"),
                "aum": (aum or {}).get("aum") if aum else None,
                "ccp": (aum or {}).get("ccp") if aum else None, "aum_real": aum_real,
            })
        clases.sort(key=lambda c: c["aum"] or 0, reverse=True)
        main = clases[0]
        f0 = recs[0]
        moneda_ccy = main["moneda"]
        tr = f0.get("tipo_renta")
        sub = subcategoria(tr, moneda_ccy, f0.get("tipo_dinero"), dur_years(f0.get("duration")),
                           f0.get("dias_liquidacion"), f0.get("fondo_nombre"),
                           f0.get("objetivo"), f0.get("region"))
        aums = {c["aum"] for c in clases if c["aum"] is not None}
        total_aum = sum(aums) if aums else None
        out.append({
            "fid": fid, "cid": main["cid"], "fondo": f0.get("fondo_nombre"),
            "soc": f0.get("sociedad"), "dep": f0.get("depositaria"),
            "cat": category_bucket(tr), "tipo": tr, "sub": sub,
            "moneda": moneda_ccy, "moneda_full": main["moneda_full"],
            "horizonte": f0.get("horizonte"), "duration": f0.get("duration"),
            "tipo_dinero": f0.get("tipo_dinero") if f0.get("tipo_dinero") not in (None, "No Aplica") else None,
            "settle": settle(f0.get("dias_liquidacion")), "dias_liq": f0.get("dias_liquidacion"),
            "n_clases": len(clases),
            "vcp": main["vcp"], "fecha": main["fecha"], "rend": main["rend"],
            "fee_admin": main["fee_admin"], "fee_in": main["fee_in"], "fee_out": main["fee_out"],
            "min": main["min"], "isin": main["isin"], "bbg": main["bbg"],
            "aum": total_aum, "ccp": main["ccp"], "aum_real": any(c["aum_real"] for c in clases),
            "obj": (f0.get("objetivo") or "")[:400], "inicio": f0.get("inicio"),
            "clases": clases,
        })
    return out
