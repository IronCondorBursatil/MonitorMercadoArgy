"""Pin de cashflows EXPLÍCITOS para los bonos con campos de estructura irregular/amortizing
(prox_cupon / amort inicio / amort cantidad) en la DB viva, sacándolos de raw_fields — paso
de DATOS del retiro de esos campos del synth/schema.

Es Δ=0 por construcción: el motor deriva todo de los CASHFLOWS materializados (no lee estos
campos), así que re-guardar los mismos flujos sin esos campos no mueve el pricing. Verifica
cashflows byte-idénticos + patas preservadas + campos fuera. Backup + restore si algo difiere.

    py -3.12 scripts/pin_irregular_to_explicit.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.web.instruments_abm import get_instrument, list_instruments, save_instrument  # noqa: E402
from config.settings import settings  # noqa: E402
from core.infrastructure.db.backup import backup_db, restore_db  # noqa: E402

DROP = ("prox_cupon", "amort inicio", "amort cantidad")


def _cfs(cfs):
    return tuple(sorted((str(c["date"])[:10], round(float(c["amortization"]), 9),
                         round(float(c["interest"]), 9)) for c in cfs))


def _tk(f):
    return {str(f.get(k)).upper() for k in ("ticker", "ticker_ars", "ticker_mep",
            "ticker_ccl") if f.get(k)}


def main() -> int:
    targets = []
    for it in list_instruments():
        g = get_instrument(it["key"]) or {}
        f = g.get("fields", {}) or {}
        if any(str(f.get(k) or "").strip() for k in DROP):
            targets.append((it["key"], g["sheet"], g))
    print(f"Bonos con {DROP}: {len(targets)}")
    if not targets:
        return 0

    bkp = backup_db(settings.catalog_db, settings.backup_dir, keep=0, tag="pre-drop-irregular")
    print(f"Backup pre-op: {bkp}\n")

    fails = []
    for tk, sheet, g in targets:
        before_cfs = _cfs(g["cashflows"])
        before_tk = _tk(g["fields"])
        fields = {k: v for k, v in g["fields"].items() if k not in DROP}
        save_instrument(sheet, fields, cashflows=g["cashflows"])
        a = get_instrument(tk) or {}
        cf_ok = _cfs(a.get("cashflows", [])) == before_cfs
        tk_ok = _tk(a.get("fields") or {}) == before_tk
        drop_ok = not any(k in (a.get("fields") or {}) for k in DROP)
        if not (cf_ok and tk_ok and drop_ok):
            print(f"  [FAIL] {tk}: cf={cf_ok} patas={tk_ok} campos_fuera={drop_ok}")
            fails.append(tk)

    if fails:
        print(f"\n⚠ {len(fails)} bonos cambiaron → RESTAURANDO backup (sin cambios).")
        restore_db(bkp, settings.catalog_db)
        return 1
    print(f"\n✓ {len(targets)} bonos pinneados explícitos; {DROP} fuera de raw_fields; "
          "cashflows byte-idénticos y patas preservadas (el motor deriva de los flujos).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
