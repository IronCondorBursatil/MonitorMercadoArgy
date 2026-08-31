# -*- coding: utf-8 -*-
"""Harness de validación de cronogramas contra el informe IAMC (28-Aug-26).

Uso:
    from validate_bond import validar
    print(validar(spec))          # spec = dict con el instrumento + cashflows

`spec` = {
  "ticker": "CO32", "short_name": "...", "instrument_type": "HARD DOLLAR",
  "sheet": "Provinciales", "isin": "...", "emission_date": "2025-07-02",
  "maturity_date": "2032-07-02", "day_count": "30/360", "payment_frequency": 2,
  "cashflows": [{"fecha_pago": "2027-01-02", "amortizacion": 0.0, "cupon_interes": 4.875}, ...]
}

NO escribe en la DB — construye el Instrument en memoria y compara contra
scratch/iamc_ref.json:
  * VR   (valor residual)  = capital NO amortizado a la fecha de liquidación
  * accr (intereses corridos) vía metrics.accrued_interest
  * VT   = VR + accr
  * TIR  vs ytm publicado (usando el precio del informe)
  * WAL  = vida promedio ponderada
Devuelve dict con ok/desvíos por métrica.
"""
import json
import os
import sys
from datetime import date, datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.domain.models import Cashflow, Instrument, MarketSnapshot  # noqa: E402
from core.domain.pricing import metrics  # noqa: E402
from core.domain.services import FinancialEngine  # noqa: E402

SETTLE = date(2026, 8, 31)          # "Fecha de Liquidación: 31/08/2026" del informe

# FX implícito del propio informe: cierre_ars / precio_usd de los globales donde IAMC
# publica AMBOS (AL30 1535.27, GD30 1535.26, AE38 1535.29, AL35 1535.31, GD41 1535.21).
# HardDollarStrategy convierte la pata en PESOS a USD por MEP (ley AR) o CCL (ley EXT);
# sin un fx provider devuelve None y los tickers que no terminan en D/C no se validaban.
_FX_IAMC = 1535.27


class _StubFx:
    """FX fijo a la fecha del informe (no toca la red)."""
    def get_mep_venta(self):
        return _FX_IAMC

    def get_ccl_venta(self):
        return _FX_IAMC

    def get_mep(self):
        return _FX_IAMC

    def get_ccl(self):
        return _FX_IAMC
# Oraculo versionado en el repo (antes vivia en scratch/, que esta gitignoreado y
# por eso el script no corria en el servidor).
_REF_PATH = os.path.join(_ROOT, "data", "iamc", "ref_2026_08_28.json")
with open(_REF_PATH, encoding="utf-8") as _f:
    REF = json.load(_f)


def _d(s):
    if isinstance(s, date):
        return s
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def build_instrument(spec: dict) -> Instrument:
    cfs = tuple(sorted(
        (Cashflow(date=_d(c["fecha_pago"]),
                  amortization=float(c.get("amortizacion", 0.0)),
                  interest=float(c.get("cupon_interes", 0.0)))
         for c in spec.get("cashflows", [])),
        key=lambda c: c.date))
    return Instrument(
        ticker=spec["ticker"], short_name=spec.get("short_name", spec["ticker"]),
        instrument_type=spec.get("instrument_type", ""),
        maturity_date=_d(spec["maturity_date"]),
        emission_date=_d(spec["emission_date"]) if spec.get("emission_date") else None,
        payment_frequency=int(spec.get("payment_frequency", 2) or 2),
        day_count=spec.get("day_count", "ACT/365"),
        cashflows=cfs,
        cer_base=spec.get("cer_base"),
        floor_rate_monthly=spec.get("floor_rate_monthly"),
        spread_rate=spec.get("spread_rate"),
        cer_spread=spec.get("cer_spread"),
        isin=spec.get("isin"),
        ley_aplicable=spec.get("ley_aplicable"),
    )


def _chk(nombre, calc, esperado, tol):
    if esperado is None:
        return {"metrica": nombre, "estado": "sin_referencia", "calc": calc}
    if calc is None:
        return {"metrica": nombre, "estado": "FALLA", "calc": None, "esperado": esperado,
                "motivo": "el motor devolvio None"}
    dif = calc - esperado
    ok = abs(dif) <= tol
    return {"metrica": nombre, "estado": "ok" if ok else "FALLA",
            "calc": round(calc, 4), "esperado": esperado, "dif": round(dif, 4), "tol": tol}


def validar(spec: dict, price: float = None, verbose: bool = True) -> dict:
    """Valida el spec contra el informe. `price` override del precio limpio."""
    t = spec["ticker"]
    ref = REF.get(t)
    if not ref:
        return {"ticker": t, "estado": "SIN_REFERENCIA_IAMC"}

    inst = build_instrument(spec)
    checks = []

    # --- VR: capital que queda por amortizar despues de la liquidacion ---
    vr_calc = sum(c.amortization for c in inst.cashflows if c.date > SETTLE)
    checks.append(_chk("VR", vr_calc, ref.get("vr"), 0.15))

    # --- accrued: intereses corridos a la fecha de liquidacion ---
    try:
        accr_calc = metrics.accrued_interest(inst, SETTLE)
    except Exception as e:  # noqa: BLE001
        accr_calc = None
        checks.append({"metrica": "accrued", "estado": "ERROR", "motivo": str(e)[:120]})
    else:
        checks.append(_chk("accrued", accr_calc, ref.get("accr"), 0.12))

    # --- VT = VR + accrued ---
    if accr_calc is not None and ref.get("vt") is not None:
        checks.append(_chk("VT", vr_calc + accr_calc, ref.get("vt"), 0.2))

    # --- TIR contra el YTM publicado ---
    # El PRECIO debe entrar en la moneda que la strategy espera:
    #  * pata …D / …C  -> ya cotiza en USD, la strategy NO divide  -> precio en USD
    #  * ticker en pesos (hard dollar sin sufijo) -> la strategy divide por MEP/CCL
    #                                             -> hay que pasarle el precio en ARS
    #  * bonos en pesos (CER/TAMAR/DL) -> `cierre` directo
    px = price
    if px is None:
        es_pata_usd = str(spec.get("ticker", "")).upper().endswith(("D", "C"))
        usd = None
        if ref.get("precio_usd") is not None:
            usd = ref["precio_usd"]
        elif ref.get("paridad") is not None and ref.get("vt") is not None:
            usd = ref["paridad"] * ref["vt"] / 100.0    # precio USD = paridad% x VT
        # SOLO HardDollarStrategy divide el precio por MEP/CCL (y solo si el ticker no
        # es una pata …D/…C, que ya cotiza en USD). Para BONAR/GLOBAL/BOPREAL y demas,
        # el precio entra en la moneda del bono — pasarles el ARS daria una TIR absurda.
        convierte = ("HARD DOLLAR" in str(spec.get("instrument_type", "")).upper()
                     and not es_pata_usd)
        if ref.get("cierre") is not None:
            px = ref["cierre"]                          # bono en pesos: directo
        elif convierte and ref.get("cierre_ars") is not None:
            px = ref["cierre_ars"]                      # la strategy lo pasa a USD
        else:
            px = usd                                    # precio en USD
    if px is not None and ref.get("ytm") is not None:
        snap = MarketSnapshot(instrument=inst, price=px)
        try:
            tir = FinancialEngine.calculate_tir(snap, settle_date=SETTLE, fx_provider=_StubFx())
            tir_pct = tir * 100.0 if tir is not None else None
        except Exception as e:  # noqa: BLE001
            tir_pct = None
            checks.append({"metrica": "TIR", "estado": "ERROR", "motivo": str(e)[:120]})
        else:
            # Tolerancia 25bp para yields normales, pero RELATIVA (1%) en los extremos:
            # en un bono que rinde 142% o -70% (muy amortizado, cotiza lejos del residual)
            # el redondeo del precio/paridad publicados ya mueve la TIR decenas de bp.
            # 25bp absolutos ahi seria exigir mas precision que la del propio informe.
            _y = abs(ref.get("ytm") or 0.0)
            _tol = max(0.25, 0.01 * _y)
            checks.append(_chk("TIR", tir_pct, ref.get("ytm"), round(_tol, 3)))

    # --- WAL: vida promedio ponderada por amortizacion ---
    futuros = [c for c in inst.cashflows if c.date > SETTLE and c.amortization > 0]
    tot = sum(c.amortization for c in futuros)
    if tot > 0 and ref.get("wal") is not None:
        wal = sum(c.amortization * ((c.date - SETTLE).days / 365.0) for c in futuros) / tot
        checks.append(_chk("WAL", wal, ref.get("wal"), 0.12))

    fallas = [c for c in checks if c["estado"] in ("FALLA", "ERROR")]
    out = {"ticker": t, "estado": "OK" if not fallas else "REVISAR",
           "n_cashflows": len(inst.cashflows), "checks": checks}
    if verbose:
        print("%-7s %-8s  %s" % (t, out["estado"], " | ".join(
            "%s:%s%s" % (c["metrica"], c.get("calc"),
                         "" if c["estado"] == "ok" else "->esp %s" % c.get("esperado"))
            for c in checks)))
    return out


if __name__ == "__main__":
    # Smoke: valida los specs de un archivo JSON {ticker: spec}
    path = sys.argv[1] if len(sys.argv) > 1 else "scratch/specs.json"
    with open(path, encoding="utf-8") as f:
        specs = json.load(f)
    ok = 0
    for tk, sp in specs.items():
        if tk.startswith("_"):
            continue
        r = validar(sp)
        ok += r["estado"] == "OK"
    print("\n%d/%d OK" % (ok, len([k for k in specs if not k.startswith('_')])))
