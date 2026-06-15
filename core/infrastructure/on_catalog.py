"""Catálogo de Obligaciones Negociables (ONs hard-dollar) → Instrument + SQLite.

Lee `data/obligaciones_negociables.csv` (términos del informe BYMA/IAMC), sintetiza
los cashflows USD (`core.domain.on_cashflows`) y da de alta UNA fila por bono:
    instrument_type = "HARD DOLLAR"   ·   category = "Obligaciones Negociables"
    day_count       = "ACT/365"  (real/365, base de intereses del calculador del broker)

Multi-ticker: cada ON se carga con las patas de moneda que existen en la API
(col `legs` del CSV: O=pesos …O, D=MEP …D, C=cable …C). Primaria = la …O (pesos)
si existe (convención multi-ticker, como Soberanos), si no la …D. Las 3 patas
comparten los cashflows USD; la …D/…C calculan bien, la …O espera la conversión
ARS→USD (otra iteración). El panel filtra a MEP (…D) por default.

`ingest()` escribe SQLite directo (igual que la ABM); es idempotente (borra la
hoja antes de re-insertar). `build_instruments()` es puro (sin I/O) → testeable.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Dict, List

from core.domain.models import Instrument
from core.domain.on_cashflows import amort_schedule, build_on_cashflows

CSV_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "obligaciones_negociables.csv"
SHEET = "Obligaciones_Negociables"
ITYPE = "HARD DOLLAR"  # las ONs del CSV son todas hard-dollar; las dollar-linked se cargan a mano
CATEGORY = "Obligaciones Negociables"
DAY_COUNT = "ACT/365"  # real/365 (base de intereses del informe); el motor lo trata como act/365.25

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_FREQ = {"S": 2, "T": 4, "A": 1, "M": 12, "V": 1}  # Vencimiento → 1 (pago único)


def parse_report_date(s: str) -> date:
    """Fecha en formato del informe: '08-Apr-38' → date(2038, 4, 8)."""
    d, mon, yy = s.strip().split("-")
    return date(2000 + int(yy), _MONTHS[mon], int(d))


def to_d_ticker(ticker_o: str) -> str:
    """El ticker de la pata USD: el …O/…P del informe pasa a …D."""
    t = (ticker_o or "").strip().upper()
    return t[:-1] + "D" if t and t[-1] in ("O", "P") else t


def row_legs(r: Dict[str, str]) -> List[str]:
    """Tickers de las patas de moneda que existen en la API, en orden ars/mep/ccl.
    La col `legs` indica cuáles: O=pesos (…O), D=MEP (…D), C=cable (…C). El primero
    es el primario. Sin `legs` → solo la pata …D (compat)."""
    o = (r["ticker_o"] or "").strip().upper()
    base = o[:-1]
    legs = (r.get("legs") or "").strip().upper()
    if not legs:
        return [to_d_ticker(o)]
    out: List[str] = []
    if "O" in legs:
        out.append(o)
    if "D" in legs:
        out.append(base + "D")
    if "C" in legs:
        out.append(base + "C")
    return out or [to_d_ticker(o)]


def load_rows(csv_path: Path = CSV_PATH) -> List[Dict[str, str]]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def row_to_instrument(r: Dict[str, str]) -> Instrument:
    estructura = r["estructura"].strip().upper()
    coupon = float(r["cupon"]) / 100.0
    cfreq = _FREQ[r["frec_cupon"].strip().upper()]
    vr = float(r["vr"])
    cuotas = int(r["cuotas_cap"])
    kfreq = _FREQ.get((r.get("frec_cap") or "").strip().upper(), 1)
    emis, vto = parse_report_date(r["emision"]), parse_report_date(r["vto"])
    prox = parse_report_date(r["prox_cap"]) if r.get("prox_cap") else vto
    next_cpn = parse_report_date(r["prox_cupon"]) if (r.get("prox_cupon") or "").strip() else None
    sched = amort_schedule(prox, vto, capital_freq=kfreq, cuotas=cuotas)
    cfs = build_on_cashflows(
        emission=emis, maturity=vto, coupon_rate=coupon, coupon_freq=cfreq,
        vr=vr, amort_dates=sched, zero_coupon=(estructura == "ZC"), next_coupon=next_cpn,
    )
    emisor = r["emisor"].strip()
    serie = (r.get("serie_clase") or "").strip()  # ej. "Clase XXXI" (listado IAMC/BYMA)
    short = f"{emisor} - {serie}" if serie else emisor
    # Base por fila (col `base`); default ACT/365. La mayoría de las ONs hard-dollar
    # liquidan intereses real/365, pero algunas (ej. Telecom Clase 24) son 30/360 —
    # el day-count cambia accrued/clean/TIR, así que NO es un detalle (ver golden).
    day_count = (r.get("base") or "").strip() or DAY_COUNT
    return Instrument(
        ticker=row_legs(r)[0], short_name=short,
        instrument_type=ITYPE, maturity_date=vto, emission_date=emis,
        cashflows=tuple(cfs), category=CATEGORY, payment_frequency=cfreq,
        day_count=day_count, ley_aplicable=(r.get("ley") or "").strip() or None,
    )


def build_instruments(csv_path: Path = CSV_PATH) -> List[Instrument]:
    return [row_to_instrument(r) for r in load_rows(csv_path)]


def abm_raw_fields(r: Dict[str, str]) -> Dict[str, object]:
    """raw_fields con las keys del schema ABM de ONs, para que el form de edición
    prefilee los términos (el `Instrument` materializado solo no los reconstruye)."""
    vto = parse_report_date(r["vto"])
    amort = int(r["cuotas_cap"]) > 1
    return {
        "short_name": r["emisor"].strip(),
        "tipo": ITYPE,
        "fecha_emision": parse_report_date(r["emision"]).isoformat(),
        "fecha_vencimiento": vto.isoformat(),
        "cupon anual %": r["cupon"].strip(),
        "frecuencia pagos": _FREQ[r["frec_cupon"].strip().upper()],
        "base calculo": (r.get("base") or "").strip() or DAY_COUNT,
        "tipo amortizacion": "amortizing" if amort else "bullet",
        "serie_clase": (r.get("serie_clase") or "").strip(),
        "ley_aplicable": (r.get("ley") or "").strip(),
    }


def ingest(csv_path: Path = CSV_PATH) -> int:
    """Da de alta las ONs en SQLite (idempotente: reemplaza la hoja). Devuelve cuántas."""
    from sqlalchemy import select
    from core.infrastructure.db.catalog_repository import init_db, instrument_to_orm
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM
    from core.infrastructure.repositories import split_currency_tickers

    rows = load_rows(csv_path)
    init_db()
    with SessionLocal.begin() as s:
        old = s.execute(select(InstrumentORM).where(InstrumentORM.sheet == SHEET)).scalars().all()
        for o in old:
            s.delete(o)
        s.flush()
        for r in rows:
            _, mep, ccl = split_currency_tickers(row_legs(r))
            s.add(instrument_to_orm(row_to_instrument(r), sheet=SHEET,
                                    raw_fields=abm_raw_fields(r), ticker_mep=mep, ticker_ccl=ccl))
    return len(rows)
