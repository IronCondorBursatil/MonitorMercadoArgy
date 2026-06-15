"""Hornea los cashflows EXPLÍCITOS de los 3 CER reestructurados (CUAP/DICP/DIP0) en la
hoja `Cashflows` del master Excel — exactamente el output ACTUAL del sintetizador (cupón
flat → schedule determinista, sin step-up ni dependencia del reloj).

El loader prioriza las filas explícitas de `Cashflows` sobre el synth (repositories.py:367),
así que tras hornear, estos 3 bonos se cargan explícitos y se puede sacar `capital_factor`
del sintetizador sin mover el pricing. Se hornea el schedule COMPLETO (incluye flujos
pasados) para que `sum(amortizaciones)` — la normalización CER de capital_factor>1 del motor
— quede idéntica.

Backup del Excel + verificación: los N instrumentos deben quedar con cashflows BYTE-idénticos
(solo cambia el ORIGEN de los 3 CER: synth→explícito, mismos valores). Restaura si algo difiere.

    py -3.12 scripts/bake_cer_cashflows.py
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

CER3 = ("CUAP", "DICP", "DIP0")


def _load():
    repo = ExcelInstrumentsRepository(str(settings.master_xlsx))
    return {i.ticker: i for i in repo.get_all_instruments()}


def _num(x):
    return 0.0 if (isinstance(x, float) and x != x) else round(x, 9)  # NaN → 0.0 (comparable)


def _snap(inst_map):
    return {tk: tuple((c.date, _num(c.amortization), _num(c.interest))
                      for c in i.cashflows) for tk, i in inst_map.items()}


def main() -> int:
    xl = Path(str(settings.master_xlsx))
    inst = _load()
    missing = [tk for tk in CER3 if tk not in inst]
    if missing:
        print(f"No están en el Excel: {missing}"); return 1
    before = _snap(inst)
    bake = {tk: [(c.date, c.amortization, c.interest) for c in inst[tk].cashflows] for tk in CER3}

    bdir = Path(str(settings.backup_dir)); bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    bkp = bdir / f"master-{stamp}-pre-bake-cer.xlsx"
    shutil.copy2(xl, bkp)
    print(f"Backup Excel: {bkp}")

    wb = openpyxl.load_workbook(xl)
    ws = wb["Cashflows"]
    added = 0
    for tk in CER3:
        for d, a, i in bake[tk]:
            ws.append([tk, d, a, i]); added += 1
    wb.save(xl); wb.close()
    print(f"Filas horneadas en Cashflows: {added} (CUAP {len(bake['CUAP'])} · "
          f"DICP {len(bake['DICP'])} · DIP0 {len(bake['DIP0'])})")

    after = _snap(_load())
    changed = [tk for tk in before if before[tk] != after.get(tk)]
    new = sorted(set(after) - set(before))
    if changed or new:
        print(f"⚠ DIFERENCIAS: cambiaron {changed[:10]} · nuevos {new} → RESTAURANDO Excel.")
        shutil.copy2(bkp, xl)
        return 1
    print(f"✓ {len(before)} instrumentos byte-idénticos. CUAP/DICP/DIP0 ahora se cargan "
          "EXPLÍCITOS (mismos valores que el synth). El loader ya no los sintetiza.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
