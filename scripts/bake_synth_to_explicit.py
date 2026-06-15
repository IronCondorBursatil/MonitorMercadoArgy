"""Hornea como cashflow EXPLÍCITO (hoja Cashflows del Excel) los bonos que hoy SINTETIZAN
con estructura irregular/amortizing — el output ACTUAL del synth, para poder retirar el
manejo de amort/prox_cupon del sintetizador sin mover el pricing.

Política: el synth queda solo para bullet (cupón regular + amort al vto) y ZC soberano;
todo lo irregular (1er cupón irregular, amortizaciones, pagos irregulares) va explícito.

BAKE = los 9 synth-afectados: 5 CER amortizing (TX26/TX28/TX31/PARP/PAP0) + 4 letras
Dolar_Linked con 1er cupón irregular y cupón nan (TZV26/D30S6/TZV27/TZV28). El interés
nan se hornea como 0.0 (el motor ya lo trata como 0 → pricing-neutral).

Backup del Excel + verificación: los N instrumentos quedan byte-idénticos (nan≡0); solo
cambia el ORIGEN de los 9 (synth→explícito). Restaura si algo difiere.

    py -3.12 scripts/bake_synth_to_explicit.py
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl  # noqa: E402
from config.settings import settings  # noqa: E402
from core.infrastructure.repositories import ExcelInstrumentsRepository  # noqa: E402

BAKE = ("TX26", "TX28", "TX31", "PARP", "PAP0", "TZV26", "D30S6", "TZV27", "TZV28")


def _n(x):
    return 0.0 if (isinstance(x, float) and x != x) else round(x, 9)  # nan → 0


def _load():
    repo = ExcelInstrumentsRepository(str(settings.master_xlsx))
    return {i.ticker: i for i in repo.get_all_instruments()}


def _snap(m):
    return {tk: tuple((c.date, _n(c.amortization), _n(c.interest)) for c in i.cashflows)
            for tk, i in m.items()}


def main() -> int:
    xl = Path(str(settings.master_xlsx))
    inst = _load()
    missing = [tk for tk in BAKE if tk not in inst]
    if missing:
        print(f"No están en el Excel: {missing}"); return 1
    before = _snap(inst)
    rows = {tk: [(c.date, _n(c.amortization), _n(c.interest)) for c in inst[tk].cashflows] for tk in BAKE}

    bdir = Path(str(settings.backup_dir)); bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    bkp = bdir / f"master-{stamp}-pre-bake-synth.xlsx"
    shutil.copy2(xl, bkp)
    print(f"Backup Excel: {bkp}")

    wb = openpyxl.load_workbook(xl)
    ws = wb["Cashflows"]
    added = 0
    for tk in BAKE:
        for d, a, i in rows[tk]:
            ws.append([tk, d, a, i]); added += 1
    wb.save(xl); wb.close()
    print(f"Filas horneadas: {added} ({', '.join(f'{tk} {len(rows[tk])}' for tk in BAKE)})")

    after = _snap(_load())
    changed = [tk for tk in before if before[tk] != after.get(tk)]
    new = sorted(set(after) - set(before))
    if changed or new:
        print(f"⚠ DIFERENCIAS: {changed[:10]} · nuevos {new} → RESTAURANDO Excel.")
        shutil.copy2(bkp, xl)
        return 1
    print(f"✓ {len(before)} instrumentos byte-idénticos. Los 9 ahora se cargan EXPLÍCITOS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
