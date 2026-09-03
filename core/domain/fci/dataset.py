"""Ensamblado del dataset del panel FCI — la forma `{meta, funds}` que consume el front.

Combina: parse enriquecido de CAFCI (unificado a 1 registro por fondo) + AUM (ArgentinaDatos)
+ macro real (lente A3500/CER) + histórico de cuotaparte (reconstruido/real) + flujos reales
mensuales (de `fci_history`, Δccp × precio de cuotaparte = patrimonio/ccp, fallback vcp/1000).
Los flujos salen SEPARADOS POR MONEDA (`flows` en pesos, `flows_usd` en dólares): nacen en la
moneda de cada CLASE y sumarlos antes de convertir mezcla unidades.
Composición NO se incluye (sin fuente pública real).

Las dependencias con I/O (store de flujos, índices) se inyectan como callables/dicts → testeable.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, Dict, List, Optional

from core.domain.fci import CAT_ORDER, HIST_N, PERIOD_LABELS, PERIODS
from core.domain.fci.derive import unify_classes
from core.domain.fci.hist import merge_real_hist, reconstruct_hist


def monthly_net_flows(daily_flows: Dict[date, float], fecha_base: Optional[str],
                      n: int = 12):
    """Bucketea flujos diarios `{date: pesos}` en los últimos `n` meses calendario
    (terminando en el mes de `fecha_base`). Devuelve (lista[n] oldest→hoy, hay_datos)."""
    base = date.fromisoformat(fecha_base) if fecha_base else date.today()
    months = []
    for i in range(n - 1, -1, -1):
        mm, yy = base.month - i, base.year
        while mm <= 0:
            mm += 12
            yy -= 1
        months.append((yy, mm))
    idx = {ym: k for k, ym in enumerate(months)}
    out = [0] * n
    for d, flow in (daily_flows or {}).items():
        k = idx.get((d.year, d.month))
        if k is not None:
            out[k] += round(flow)
    return out, bool(daily_flows)


def flows_by_ccy(daily) -> Dict[str, Dict[date, float]]:
    """Normaliza lo que devuelve `flows_lookup`: `{moneda: {date: flow}}` tal cual, o un
    `{date: flow}` plano (forma vieja / lookups de test) leído como pesos.

    La forma con moneda existe porque el merge de clases se hace en el server: un fondo
    puede tener clases en pesos y en dólares (95 de 1.096 en el corte real) y el flujo de
    cada clase nace en SU moneda."""
    if not daily:
        return {}
    first = next(iter(daily.values()))
    if isinstance(first, dict):
        return {c: s for c, s in daily.items() if s}
    return {"ARS": daily}


def hist_axis(base: date, n: int = HIST_N, span: int = 364) -> List[str]:
    return [(base - timedelta(days=round((n - 1 - i) * span / (n - 1)))).isoformat()
            for i in range(n)]


def build_fci_dataset(parsed_funds: List[dict], aum_index: Dict[str, dict], macro: dict,
                      *, flows_lookup: Optional[Callable] = None,
                      hist_lookup: Optional[Callable] = None,
                      fecha_base: Optional[str] = None,
                      generated_at: Optional[str] = None) -> dict:
    """Arma `{meta, funds}`. `flows_lookup(fondo)->{moneda:{date:flow}}` y
    `hist_lookup(fondo)->{date:{vcp}}` leen `fci_history` (vacíos al inicio → flujos en
    'acumulando' e hist reconstruido).

    Flujos por fondo: `flows` (12 meses, en PESOS: las clases en pesos) y `flows_usd`
    (mismos 12 meses, en DÓLARES: las clases en dólares), presente solo si hay flujo en
    esa moneda. El front los agrega convirtiendo la pata USD al MEP del corte; nunca se
    suman entre sí acá."""
    funds = unify_classes(parsed_funds, aum_index)
    base = date.fromisoformat(fecha_base) if fecha_base else date.today()
    axis = hist_axis(base)
    any_flows_real = False
    for f in funds:
        key = str(f["fid"])
        rec_hist = reconstruct_hist(f["vcp"], f["rend"], key)
        real_series = (hist_lookup(f["fondo"]) if hist_lookup else {}) or {}
        f["hist"], f["hist_real"] = merge_real_hist(rec_hist, real_series, axis)
        by_ccy = flows_by_ccy(flows_lookup(f["fondo"]) if flows_lookup else None)
        f["flows"], fr_ars = monthly_net_flows(by_ccy.get("ARS"), fecha_base)
        flows_usd, fr_usd = monthly_net_flows(by_ccy.get("USD"), fecha_base)
        if fr_usd:
            f["flows_usd"] = flows_usd
        f["flows_real"] = fr_ars or fr_usd
        any_flows_real = any_flows_real or f["flows_real"]

    funds.sort(key=lambda f: (CAT_ORDER.index(f["cat"]) if f["cat"] in CAT_ORDER else 99,
                              -(f["aum"] or 0)))
    subs: Dict[str, set] = {}
    for f in funds:
        subs.setdefault(f["cat"], set()).add(f["sub"])

    meta = {
        "fecha_base": fecha_base, "generated_at": generated_at,
        "periods": list(PERIODS), "period_labels": PERIOD_LABELS,
        "cat_order": CAT_ORDER,
        "subs_by_cat": {c: sorted(subs.get(c, set())) for c in CAT_ORDER},
        "hist_axis": axis, "macro": macro,
        "n_total": len(funds), "n_shown": len(funds),
        "n_aum_real": sum(1 for f in funds if f["aum_real"]),
        "flows_real": any_flows_real,
        "sources": {
            "catalogo_retornos_vcp": "CAFCI (real)",
            "aum": "ArgentinaDatos (real, join por nombre)",
            "macro": "A3500 (proxy FX) + CER reales (BCRA)",
            "hist": "real (fci_history) cuando cubre la ventana; si no, reconstruido de los retornos reales",
            "flujos": ("real (fci_history, Δcuotapartes × precio de cuotaparte, separado por "
                       "moneda de clase); 'acumulando…' hasta juntar ruedas"),
            "composicion": "no disponible (sin fuente pública)",
        },
    }
    return {"meta": meta, "funds": funds}
