"""Sincroniza el catálogo de letras capitalizables con la API de ArgentinaDatos.

    py -3.12 scripts/sync_letras.py              # DRY RUN: sólo dice qué haría
    py -3.12 scripts/sync_letras.py --apply      # da de alta las letras nuevas
    py -3.12 scripts/sync_letras.py --json       # para scriptear

QUÉ HACE. Trae `GET /v1/finanzas/letras`, lo compara con las LECAP/BONCAP que ya
están en la base y **da de alta las que faltan**. Cada alta va por
`instruments_abm.save_instrument`, el mismo camino que usa la ABM, con sus guards.

QUÉ NO HACE, a propósito (el detalle y el porqué está en
`core/infrastructure/letras_sync.py`):

* **No pisa** una letra que ya está: el catálogo es la fuente de verdad y sus datos
  salen de IAMC/BYMA, más ricos que los de la API —que manda `tem` en 0 y
  `fechaEmision` vacía en 12 de 18 filas—. Las diferencias se reportan.
* **No borra** nada: que la API deje de listar una letra no significa que se haya
  ido del mundo.
* **No inventa** una fecha de emisión: sin ella la ABM rechaza el alta, no se puede
  deducir, y la letra queda listada para cargarla a mano.

DÓNDE CORRE. Contra `settings.db_dir`, que lo imprime antes de nada. En el servidor
`MONITOR_DB_DIR` vive en el drop-in de systemd y **una shell manual NO lo hereda**:

    MONITOR_DB_DIR=/var/lib/monitor venv/bin/python scripts/sync_letras.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _imprimir(plan, aplicado: bool) -> None:
    print()
    if plan.rechazado:
        print("PAYLOAD RECHAZADO: %s" % plan.rechazado)
        print("  No se decidió nada sobre el catálogo. Reintentar más tarde.")
        return

    if plan.altas:
        titulo = "ALTAS APLICADAS" if aplicado else "ALTAS QUE HARÍA (dry run)"
        print("%s (%d):" % (titulo, len(plan.altas)))
        for a in plan.altas:
            estado = ""
            if aplicado:
                estado = "  OK" if a["ticker"] in plan.aplicadas else "  ERROR: %s" % a.get("error", "?")
            print("  %-8s %-7s emision=%s vto=%s pago=%.4f%s"
                  % (a["ticker"], a["clase"], a["fecha_emision"], a["fecha_pago"],
                     a["pago"], estado))
    else:
        print("Sin altas: no hay letras nuevas y completas en la API.")

    if plan.incompletas:
        print("\nNUEVAS PERO INCOMPLETAS (%d) — cargar a mano por la ABM:"
              % len(plan.incompletas))
        for i in plan.incompletas:
            print("  %-8s vto=%s  falta: %s" % (i["ticker"], i["vto"], i["falta"]))

    if plan.diferencias:
        print("\nDIFERENCIAS con lo que ya está (%d) — NO se tocan, revisalas vos:"
              % len(plan.diferencias))
        for d in plan.diferencias:
            print("  %-8s %s" % (d["ticker"], "; ".join(d["difs"])))

    if plan.invalidas:
        print("\nFILAS DESCARTADAS DE LA API (%d):" % len(plan.invalidas))
        for i in plan.invalidas:
            print("  %-8s %s" % (i["ticker"], i["motivo"]))

    print("\n%s" % plan.resumen())
    if plan.vencidas:
        print("  (%d vencidas en el payload, ignoradas: %s)"
              % (len(plan.vencidas), ", ".join(v["ticker"] for v in plan.vencidas)))
    if plan.solo_en_catalogo:
        print("  (%d en el catálogo que la API no lista, se conservan: %s)"
              % (len(plan.solo_en_catalogo), ", ".join(plan.solo_en_catalogo)))
    if plan.altas and not aplicado:
        print("\nPara aplicarlas:  py -3.12 scripts/sync_letras.py --apply")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="da de alta de verdad (sin esto sólo informa)")
    ap.add_argument("--json", action="store_true", help="salida JSON")
    args = ap.parse_args(argv)

    from config.settings import settings

    db = Path(settings.catalog_db)
    if not args.json:
        print("catalogo: %s" % db)
    if not db.is_file():
        print("ABORTADO: no hay catalog.db en ese directorio.")
        print("  Casi seguro es el db_dir equivocado. En el servidor:")
        print("      MONITOR_DB_DIR=/var/lib/monitor venv/bin/python scripts/sync_letras.py")
        return 4

    from apps.web.letras_service import sincronizar

    plan = sincronizar(aplicar=args.apply, hoy=date.today())

    if args.json:
        print(json.dumps({
            "rechazado": plan.rechazado,
            "altas": [a["ticker"] for a in plan.altas],
            "aplicadas": plan.aplicadas,
            "incompletas": [i["ticker"] for i in plan.incompletas],
            "diferencias": plan.diferencias,
            "invalidas": plan.invalidas,
            "vencidas": [v["ticker"] for v in plan.vencidas],
            "solo_en_catalogo": plan.solo_en_catalogo,
            "sin_cambios": plan.sin_cambios,
        }, indent=2, ensure_ascii=False))
    else:
        _imprimir(plan, aplicado=args.apply)

    if plan.rechazado:
        return 3
    # Un alta que falló no puede salir 0: si esto corre en un timer, el operador se
    # entera por el exit code, no leyendo el log.
    if args.apply and len(plan.aplicadas) != len(plan.altas):
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
