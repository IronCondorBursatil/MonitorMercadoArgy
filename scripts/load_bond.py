"""Carga un bono al ABM aplicando el FILTRO synth-vs-explícito (decisión objetiva,
no a ojo). Pensado para el workflow "subo una imagen con los términos + el cashflow
y la IA lo carga": la IA extrae los campos del form y (si está) el cashflow explícito
de la imagen, y llama `load_bond(...)`.

El filtro (ver memoria load-bond-from-image):

  1. Sintetiza el schedule desde los params, por el MISMO camino que el ABM (_safe_synth).
  2. Si la imagen trae el cashflow explícito → reconcilia synth vs explícito fila a fila:
       · coincide dentro de tolerancia → carga por SYNTH (params; re-derivable, barato,
         convención central de redondeo/day-count/ex-cupón).
       · diverge → carga EXPLÍCITO (el de la imagen): es un no-sintetizable (stub que
         devenga, amort a medida, etc.). El reporte dice EXACTAMENTE qué difiere.
  3. Sin cashflow explícito → synth-only (se valida contra la TIR/paridad publicada con verify()).

Seguridad: backup incondicional pre-alta (snapshot tagged, no rota), y `save_instrument`
(append/upsert transaccional, NO destructivo). NUNCA usa on_catalog.ingest.

Uso típico (desde la raíz del repo, py -3.12):

    from scripts.load_bond import load_bond, verify
    res = load_bond("Obligaciones_Negociables", FIELDS, explicit_cfs=IMG_CASHFLOW)
    print(res["mode"], res["decision"])          # 'synth'|'explicit' + el porqué
    print(verify(res["ticker"], price=98.5))     # TIR/MD/paridad para chequear vs la imagen
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

# Tolerancias de la reconciliación (en términos de face 100). El cashflow del broker
# suele venir redondeado a centavos; un desvío > tol en algún flujo = no-sintetizable.
_TOL_AMORT = 0.02
_TOL_INTEREST = 0.02


def _to_date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"fecha no parseable: {v!r}")


def _f(v: Any) -> float:
    if v is None or v == "":
        return 0.0
    return round(float(v), 6)


def synth_schedule(fields: Dict[str, Any]) -> List[Tuple[date, float, float]]:
    """Schedule sintetizado (date, amort, interest) por el mismo camino del ABM."""
    from apps.web.instruments_abm import _safe_synth
    return sorted((cf.date, round(cf.amortization, 6), round(cf.interest, 6))
                  for cf in _safe_synth(fields))


def _explicit_rows(cfs: List[Dict[str, Any]]) -> List[Tuple[date, float, float]]:
    """Normaliza el cashflow explícito de la imagen → [(date, amort, interest)]."""
    out = []
    for r in cfs:
        out.append((_to_date(r["date"]),
                    _f(r.get("amortization", r.get("amort", 0))),
                    _f(r.get("interest", r.get("cupon", r.get("coupon", 0))))))
    return sorted(out)


def reconcile_synth_vs_explicit(
    fields: Dict[str, Any], explicit_cfs: List[Dict[str, Any]],
    *, tol_amort: float = _TOL_AMORT, tol_interest: float = _TOL_INTEREST,
) -> Tuple[bool, str]:
    """¿El synth reproduce el cashflow de la imagen? (match, reporte legible)."""
    synth = synth_schedule(fields)
    expl = _explicit_rows(explicit_cfs)
    sd = {d: (a, i) for d, a, i in synth}
    ed = {d: (a, i) for d, a, i in expl}
    all_dates = sorted(set(sd) | set(ed))
    diffs: List[str] = []
    for d in all_dates:
        sa, si = sd.get(d, (None, None))
        ea, ei = ed.get(d, (None, None))
        if d not in sd:
            diffs.append(f"  {d}: SOLO explícito (amort={ea} int={ei}) — el synth no genera este flujo")
        elif d not in ed:
            diffs.append(f"  {d}: SOLO synth (amort={sa} int={si}) — el synth genera un flujo de más")
        elif abs(sa - ea) > tol_amort or abs(si - ei) > tol_interest:
            diffs.append(f"  {d}: amort {sa} vs {ea} (Δ{round(sa-ea,4)}) · int {si} vs {ei} (Δ{round(si-ei,4)})")
    if not diffs:
        return True, (f"synth ≡ explícito ({len(synth)} flujos, tol amort {tol_amort} / int {tol_interest}) "
                      "→ se carga por SYNTH (re-derivable).")
    head = (f"synth ≠ explícito ({len(synth)} synth vs {len(expl)} img) → se carga EXPLÍCITO. "
            "Diferencias:\n")
    return False, head + "\n".join(diffs)


def _backup() -> Optional[str]:
    from config.settings import settings
    from core.infrastructure.db.backup import backup_db
    try:
        p = backup_db(settings.catalog_db, settings.backup_dir, keep=0, tag="pre-load-bond")
        return str(p) if p else None
    except Exception as e:  # noqa: BLE001 — el backup no debe abortar el alta manual
        return f"(backup falló: {e})"


def load_bond(
    sheet: str, fields: Dict[str, Any], *,
    explicit_cfs: Optional[List[Dict[str, Any]]] = None,
    force_explicit: bool = False, backup: bool = True,
) -> Dict[str, Any]:
    """Aplica el filtro y carga el bono. Devuelve {ticker, mode, decision, backup,
    save, schedule}. `mode` = 'synth' | 'explicit'. `schedule` = el cashflow EFECTIVO
    que quedó cargado (para comparar de un vistazo con la imagen)."""
    from apps.web.instruments_abm import save_instrument

    decision = ""
    if force_explicit and explicit_cfs:
        mode, chosen = "explicit", explicit_cfs
        decision = "force_explicit=True → EXPLÍCITO sin reconciliar."
    elif explicit_cfs:
        match, decision = reconcile_synth_vs_explicit(fields, explicit_cfs)
        mode, chosen = ("synth", None) if match else ("explicit", explicit_cfs)
    else:
        mode, chosen = "synth", None
        decision = "sin cashflow explícito en la imagen → SYNTH (validar con verify())."

    bkp = _backup() if backup else None
    save = save_instrument(sheet, fields, cashflows=chosen)

    # Schedule efectivo cargado (releer del bono guardado).
    from apps.web.instruments_abm import get_instrument
    loaded = get_instrument(save["ticker"]) or {}
    schedule = loaded.get("cashflows", [])
    return {"ticker": save["ticker"], "mode": mode, "decision": decision,
            "backup": bkp, "save": save, "schedule": schedule}


class _NoQuotes:
    """Provider stub: la verificación usa el PRECIO provisto, no quotes live."""
    def fetch_snapshots(self, tickers):
        return {}


def verify(ticker: str, *, price: Optional[float] = None, price_mode: str = "dirty",
           tir: Optional[float] = None, settlement_lag: int = 1) -> Optional[Dict[str, Any]]:
    """Reconciliación contra ground-truth: recalcula TIR/MD/paridad del bono cargado a un
    precio (o TIR) dado, para comparar con lo publicado en la imagen (Balanz/IAMC).
    settlement_lag=1 (T+1) es la convención de la calculadora. Best-effort (usa índices/FX
    reales con fallback offline)."""
    from apps.web.bond_detail import calculate
    from core.infrastructure.db.catalog_repository import CatalogRepository
    from core.infrastructure.fx_provider import DolarAPIProvider
    from core.infrastructure.indices_provider import BCRAIndicesProvider
    try:
        repo = CatalogRepository(auto_seed=False)
        indices = BCRAIndicesProvider(excel_repo=repo)
        fx = DolarAPIProvider()
        mode = "from_tir" if (tir is not None and price is None) else "from_price"
        return calculate(ticker, repo, _NoQuotes(), indices, fx,
                         mode=mode, price=price, price_mode=price_mode, tir=tir,
                         settlement_lag=settlement_lag)
    except Exception as e:  # noqa: BLE001 — verify es best-effort
        return {"error": f"{type(e).__name__}: {e}"}
