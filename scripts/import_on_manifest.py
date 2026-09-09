"""py -3.12 scripts/import_on_manifest.py manifest.json --db PATH [--apply].

Alta manual de ON verificadas: dry-run por defecto, respaldo obligatorio, sin upsert.
En prod MONITOR_DB_DIR debe ser explícito. El manifiesto contiene flujos revisados,
no fórmulas a sintetizar. Una coincidencia ajena o alterada aborta todo el preflight.
Cada alta usa la transacción del ABM; el lote NO es atómico. Se imprime cada alta
confirmada y se conserva el snapshot para recuperación manual, nunca automática.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
from datetime import date

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.domain.currency import ccy_from_suffix  # noqa: E402
from core.domain.on_classification import SECTOR_MAP  # noqa: E402

SHEET = "Obligaciones_Negociables"
_SLOTS = ("ticker_ars", "ticker_mep", "ticker_ccl")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False).encode()).hexdigest()


def tickers(fields):
    return [fields[k] for k in _SLOTS if fields.get(k)]


def _number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: debe ser un número explícito")
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"{label}: monto inválido")
    return value


def validate(manifest):
    if manifest.get("schema_version") != 1:
        raise ValueError("schema_version debe ser 1")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", manifest.get("batch_id", "")):
        raise ValueError("batch_id inválido")
    if not re.fullmatch(r"[a-f0-9]{64}", manifest.get("source_sha256", "")):
        raise ValueError("source_sha256 inválido")
    seen_tickers, seen_isins = set(), set()
    if not isinstance(manifest.get("records"), list):
        raise ValueError("records debe ser una lista")
    for record in manifest["records"]:
        f, cfs = record["fields"], record["cashflows"]
        if any(k != k.strip().lower() for k in f):
            raise ValueError("Las claves de fields deben estar normalizadas")
        ts = tickers(f)
        if not ts or any(not re.fullmatch(r"[A-Z0-9]{2,12}", t) for t in ts):
            raise ValueError("ticker faltante o inválido")
        if len(ts) != len(set(ts)) or seen_tickers.intersection(ts):
            raise ValueError(f"ticker duplicado: {ts}")
        seen_tickers.update(ts)
        for slot, ccy in zip(_SLOTS, ("ARS", "MEP", "CABLE")):
            if f.get(slot) and ccy_from_suffix(f[slot]) != ccy:
                raise ValueError(f"{slot}: sufijo de moneda incorrecto")
        isin = f.get("isin")
        if isin:
            if not re.fullmatch(r"[A-Z0-9]{12}", isin):
                raise ValueError(f"ISIN inválido: {isin}")
            if isin in seen_isins:
                raise ValueError(f"ISIN duplicado: {isin}")
            seen_isins.add(isin)
        if f.get("tipo") not in ("HARD DOLLAR", "DOLLAR LINKED"):
            raise ValueError(f"{ts}: tipo ON inválido")
        if f.get("sector_override") not in SECTOR_MAP:
            raise ValueError(f"{ts}: sector inválido")
        if f.get("ley_aplicable") not in ("Argentina", "Extranjera"):
            raise ValueError(f"{ts}: ley no verificada")
        if not f.get("short_name"):
            raise ValueError(f"{ts}: emisor faltante")
        if f.get("base calculo") not in ("30/360", "ACT/365", "ACT/365.25", "ACT/ACT"):
            raise ValueError(f"{ts}: base inválida")
        if f.get("frecuencia pagos") not in (1, 2, 4, 12):
            raise ValueError(f"{ts}: frecuencia inválida")
        emitted, maturity = date.fromisoformat(f["fecha_emision"]), date.fromisoformat(f["fecha_vencimiento"])
        if emitted >= maturity or not cfs:
            raise ValueError(f"{ts}: fechas o flujos inválidos")
        previous = emitted
        for cf in cfs:
            if set(cf) != {"date", "amortization", "interest"}:
                raise ValueError(f"{ts}: campos de cashflow inválidos")
            pay = date.fromisoformat(cf["date"])
            if not previous < pay <= maturity:
                raise ValueError(f"{ts}: fechas de flujos no ordenadas/duplicadas/fuera de plazo")
            previous = pay
            for key in ("amortization", "interest"):
                _number(cf[key], f"{ts}: {key}")
        capital = _number(record["expected_principal"], f"{ts}: capital", positive=True)
        # Una cláusula preceding puede adelantar el pago sin cambiar el vencimiento
        # legal. Exige fecha explícita verificada, no infiere ni desplaza calendarios.
        final_payment = date.fromisoformat(f.get("fecha_ultimo_pago_contractual", f["fecha_vencimiento"]))
        if not emitted < final_payment <= maturity or previous != final_payment or not math.isclose(sum(c["amortization"] for c in cfs), capital, abs_tol=1e-7):
            raise ValueError(f"{ts}: capital o último flujo no concuerda")
        if not record.get("sources") or any(not s.get("url", "").startswith("https://") for s in record["sources"]):
            raise ValueError(f"{ts}: falta procedencia")
        digest(record)  # Rechaza NaN/Inf también en metadata.
    return manifest


def inventory(db_path):
    """Sólo instrumentos y flujos; no inicializa schema ni lee cuentas/secretos."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return {table: [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
                for table, order in (("instruments", "ticker"), ("cashflows", "ticker,fecha_pago,id"))}


def assert_preserved(before, after):
    for table, key in (("instruments", "ticker"), ("cashflows", "id")):
        actual = {r[key]: r for r in after[table]}
        for row in before[table]:
            if actual.get(row[key]) != row:
                raise RuntimeError(f"Se modificó una fila previa: {table}/{row[key]}")


def persisted_fields(manifest, record):
    return {**record["fields"], "alta_lote_id": manifest["batch_id"],
            "alta_registro_sha256": digest(record), "alta_fuente_sha256": manifest["source_sha256"],
            "alta_fuentes": record["sources"], "alta_notas": record.get("notes", [])}


def _is_exact(row, state, manifest, record):
    raw = json.loads(row["raw_fields"]) if isinstance(row["raw_fields"], str) else row["raw_fields"]
    if any(raw.get(k) != v for k, v in persisted_fields(manifest, record).items()):
        return False
    expected_columns = {
        "sheet": SHEET, "instrument_type": record["fields"]["tipo"],
        "emission_date": record["fields"]["fecha_emision"],
        "maturity_date": record["fields"]["fecha_vencimiento"],
        "payment_frequency": record["fields"]["frecuencia pagos"],
        "day_count": record["fields"]["base calculo"],
        "isin": record["fields"].get("isin"),
        "short_name": record["fields"]["short_name"],
        "category": "Obligaciones Negociables",
    }
    if any(row.get(k) != v for k, v in expected_columns.items()):
        return False
    primary = tickers(record["fields"])[0]
    expected_slots = {"ticker": primary, **{
        k: record["fields"].get(k) if record["fields"].get(k) != primary else None
        for k in ("ticker_mep", "ticker_ccl")}}
    if any(row.get(k) != v for k, v in expected_slots.items()):
        return False
    stored = [c for c in state["cashflows"] if c["ticker"] == row["ticker"]]
    if any(c["es_ancla"] for c in stored):
        return False
    cfs = [{"date": c["fecha_pago"], "amortization": c["amortizacion"], "interest": c["cupon_interes"]}
           for c in stored]
    return cfs == record["cashflows"]


def plan(manifest, state):
    additions, skipped = [], []
    for record in manifest["records"]:
        ts, isin = set(tickers(record["fields"])), record["fields"].get("isin")
        matches = [r for r in state["instruments"]
                   if ts.intersection(r.get(k) for k in ("ticker", "ticker_mep", "ticker_ccl"))
                   or (isin and isin == (r.get("isin") or "").strip().upper())]
        if not matches:
            additions.append(record)
        elif len(matches) == 1 and _is_exact(matches[0], state, manifest, record):
            skipped.append(tickers(record["fields"])[0])
        else:
            raise ValueError(f"coincidencia existente: {sorted(ts)}; no se sobrescribe")
    return additions, skipped


def run_import(manifest, db_path, *, apply=False):
    validate(manifest)
    db_path = Path(db_path).resolve()
    before = inventory(db_path)
    additions, skipped = plan(manifest, before)
    report = {"database": str(db_path), "batch_id": manifest["batch_id"],
              "planned": [tickers(r["fields"])[0] for r in additions],
              "skipped": skipped, "created": [], "backup": None, "dry_run": not apply}
    if not apply or not additions:
        return report
    from config.settings import settings
    from scripts.op_guards import guard_write_snapshot

    if Path(settings.catalog_db).resolve() != db_path:
        raise ValueError("--db y MONITOR_CATALOG_DB/settings.catalog_db deben coincidir")
    rc, snapshot = guard_write_snapshot("pre-" + manifest["batch_id"], force=False)
    if rc or not snapshot or not Path(snapshot).is_file():
        raise RuntimeError(f"preflight abortado ({rc}): no se escribió")
    report["backup"] = str(snapshot)
    if inventory(snapshot) != before or inventory(db_path) != before:
        raise RuntimeError("El catálogo cambió durante el preflight; no se escribió")

    from core.infrastructure.db import engine as db_engine
    from apps.web.instruments_abm import save_instrument

    db_engine.configure(db_path)
    for record in additions:
        current = inventory(db_path)
        assert_preserved(before, current)
        remaining, _ = plan({**manifest, "records": [record]}, current)
        if not remaining:
            raise RuntimeError("El catálogo cambió durante el lote; se interrumpió la carga")
        result = save_instrument(SHEET, persisted_fields(manifest, record),
                                 cashflows=record["cashflows"], create_only=True)
        if result["action"] != "created":
            raise RuntimeError("ABM informó modificación inesperada; revisar snapshot antes de continuar")
        report["created"].append(result["ticker"])
        print(f"Alta confirmada: {result['ticker']}")
    after = inventory(db_path)
    assert_preserved(before, after)
    remaining, exact = plan(manifest, after)
    if remaining or len(exact) != len(manifest["records"]):
        raise RuntimeError("Falló la relectura del manifiesto")
    report["previous_instruments_preserved"] = len(before["instruments"])
    report["previous_cashflows_preserved"] = len(before["cashflows"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    print(f"Catálogo objetivo: {args.db.resolve()}")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = run_import(manifest, args.db, apply=args.apply)
    serialized = json.dumps(result, indent=2, ensure_ascii=False)
    if args.report:
        args.report.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
