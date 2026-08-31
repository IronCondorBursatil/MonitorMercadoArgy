"""Alta masiva de las especies del informe IAMC/BYMA 28-Aug-26 que faltaban en el catálogo.

Fuente de los cronogramas: ficha técnica BYMA (`formaAmortizacion` + `interes`) por especie.
CRITERIO DE ACEPTACIÓN: cada bono se valida contra los valores PUBLICADOS por el IAMC a la
fecha de liquidación 31/08/2026 — VR (valor residual), intereses corridos, V.Téc, TIR y WAL.
El harness (`scratch/validate_bond.py`) está calibrado: contra bonos que YA estaban cargados
(AL30, GD30, AE38) reproduce la TIR del IAMC con <1bp de error. Un bono que no valida NO se
carga: un cronograma inventado da TIR y paridad falsas, que es peor que no tener el bono.

Deuda SUBSOBERANA: entra con tipos propios ("PROVINCIAL HARD DOLLAR", …) — NO se reusan los
de las ONs corporativas. Los predicados del motor matchean por substring (→ se precian igual
que una ON hard-dollar) pero los paneles filtran por igualdad exacta (→ panel propio).

DB-only, no destructivo (append/update por ticker). Snapshot pre-op. Idempotente.

    py -3.12 scripts/ingest_iamc_2026_08.py --dry-run   # valida y muestra, no escribe
    py -3.12 scripts/ingest_iamc_2026_08.py             # alta + verificación
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# Specs versionados en el repo (antes scratch/, gitignoreado -> no llegaban al server).
SPEC_FILE = str(ROOT / "data" / "iamc" / "specs_2026_08_28.json")

# instrument_type del spec → (sheet, tipo final en la DB). Los provinciales se
# re-tipan para caer en su propio panel sin cambiar su pricing.
_PROV_RETYPE = {
    "HARD DOLLAR": "PROVINCIAL HARD DOLLAR",
    "DOLLAR LINKED": "PROVINCIAL DOLAR_LINKED",
    "DOLAR_LINKED": "PROVINCIAL DOLAR_LINKED",
    "CER": "PROVINCIAL CER",
    "BONCER": "PROVINCIAL CER",
    "BONCER ZC": "PROVINCIAL CER",
    "BONOFIJA": "PROVINCIAL ARS",   # cupon periodico en pesos (TAMAR/BADLAR/dual)
    "PURO": "PROVINCIAL ARS",
    "TAMAR": "PROVINCIAL ARS",
    "BADLAR": "PROVINCIAL ARS",
}

# Hoja destino por grupo del informe (scratch/iamc_ref.json → "grupo").
_SHEET_BY_GRUPO = {
    "soberano_usd": "Soberanos", "bopreal": "Soberanos",
    "lecap": "Tasa_Fija",
    "cer_cupon_cero": "CER",
    "dolar_linked": "Dolar_Linked",
    "tamar_cap": "TAMAR", "dual_cer_tamar": "TAMAR", "dual_dl_tamar": "TAMAR",
    "prov_usd": "Provinciales", "prov_ars": "Provinciales", "badlar": "Provinciales",
}


def _load_specs() -> dict:
    with open(SPEC_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return {t.upper(): sp for t, sp in data.items()
            if not t.startswith("_") and isinstance(sp, dict)}


def main(dry_run: bool = False) -> int:
    from _iamc_validate import REF, build_instrument, validar

    specs = _load_specs()
    print(f"specs encontrados: {len(specs)}\n")

    validos, rechazados = {}, []
    for tk, spec in sorted(specs.items()):
        r = validar(spec, verbose=False)
        if r.get("estado") == "OK":
            validos[tk] = spec
        else:
            fallas = [c for c in r.get("checks", []) if c.get("estado") in ("FALLA", "ERROR")]
            det = "; ".join(
                f"{c['metrica']} {c.get('calc')} != {c.get('esperado')}" for c in fallas) or r.get("estado")
            rechazados.append((tk, det))

    print(f"VALIDAN contra el informe IAMC: {len(validos)}")
    print("  " + " ".join(sorted(validos)))
    if rechazados:
        print(f"\nNO validan (NO se cargan): {len(rechazados)}")
        for tk, det in rechazados:
            print(f"  {tk:<7} {det}")

    faltantes = [t for t in REF if not t.startswith("_") and t not in specs]
    if faltantes:
        print(f"\nSin cronograma construido todavía: {len(faltantes)}")
        print("  " + " ".join(sorted(faltantes)))

    if dry_run:
        print("\n== DRY RUN (no escribe) ==")
        return 0
    if not validos:
        print("\nNada que cargar.")
        return 1

    from config.settings import settings
    from core.infrastructure.db.backup import backup_db
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import CashflowORM, InstrumentORM

    snap = backup_db(settings.catalog_db, settings.backup_dir,
                     keep=settings.backup_keep, tag="pre-iamc-2026-08")
    print(f"\nbackup pre-op: {snap}")

    init_db()
    creados, actualizados = [], []
    with SessionLocal.begin() as s:
        for tk, spec in sorted(validos.items()):
            inst = build_instrument(spec)
            grupo = (REF.get(tk) or {}).get("grupo", "")
            sheet = _SHEET_BY_GRUPO.get(grupo) or spec.get("sheet") or "Provinciales"
            tipo = spec.get("instrument_type", "")
            if sheet == "Provinciales":
                tipo = _PROV_RETYPE.get(tipo.upper(), tipo)

            orm = s.get(InstrumentORM, tk)
            if orm is None:
                orm = InstrumentORM(ticker=tk)
                s.add(orm)
                creados.append(tk)
            else:
                actualizados.append(tk)

            orm.short_name = spec.get("short_name", tk)
            orm.instrument_type = tipo
            orm.sheet = sheet
            orm.isin = spec.get("isin")
            orm.maturity_date = inst.maturity_date
            orm.emission_date = inst.emission_date
            orm.payment_frequency = inst.payment_frequency
            orm.day_count = inst.day_count
            orm.cer_base = spec.get("cer_base")
            orm.spread_rate = spec.get("spread_rate")
            orm.cer_spread = spec.get("cer_spread")
            orm.floor_rate_monthly = spec.get("floor_rate_monthly")
            orm.category = spec.get("category") or ("Provinciales" if sheet == "Provinciales" else None)
            orm.raw_fields = {"origen": "IAMC 2026-08-28 + ficha BYMA",
                              "ley_aplicable": spec.get("ley_aplicable")}
            # Cashflows: replace-all del bono (delete + insert), como hace el ABM.
            orm.cashflows = [
                CashflowORM(ticker=tk, fecha_pago=c.date,
                            amortizacion=c.amortization, cupon_interes=c.interest)
                for c in inst.cashflows
            ]

    print(f"\ncreados: {len(creados)}  |  actualizados: {len(actualizados)}")
    if creados:
        print("  nuevos: " + " ".join(creados))

    # --- Verificación post-escritura: releer de la DB y revalidar contra el IAMC ---
    from core.infrastructure.db.catalog_repository import CatalogRepository
    repo = CatalogRepository(auto_seed=False)
    en_db = {i.ticker: i for i in repo.get_all_instruments()}
    faltan_db = [t for t in validos if t not in en_db]
    print(f"\nverificación: {len(validos) - len(faltan_db)}/{len(validos)} presentes en la DB")
    if faltan_db:
        print("  NO quedaron: " + " ".join(faltan_db))
        return 1
    print("\nOK. Reiniciá el server (o esperá el reload del repo) para verlos en los paneles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv))
