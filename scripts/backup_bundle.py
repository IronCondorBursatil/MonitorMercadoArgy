"""Bundle de TODO el estado del servidor, para sacarlo de la caja.

    py -3.12 scripts/backup_bundle.py                 # a db_dir/backups/offsite/
    py -3.12 scripts/backup_bundle.py --out /tmp/x    # a otro lado
    py -3.12 scripts/backup_bundle.py --keep 5

POR QUE EXISTE. `backup_db` respalda `catalog.db` — y sólo eso, al MISMO disco. Fuera
quedaban los cuatro históricos (`price_history` con 199k filas, `fci_history`,
`ratings_history`, `index_history`), que **se acumulan rueda a rueda y no se
backfillean**: si se pierden, se perdieron. También quedaban afuera el `jwt_secret`
(sin él, todas las sesiones mueren), el `.env` con las credenciales BYMA,
`cartera.json` con las tenencias del usuario y `dashboard_layout.json`.

Con una instancia Always Free que Oracle puede **reclamar por inactividad**, "todo en
el mismo disco" no es una imprudencia teórica.

SEGURO CON LA APP CORRIENDO. Usa `sqlite3.Connection.backup` (vía `_online_copy` de
`core/infrastructure/db/backup.py`), que es consistente con WAL: copiar el `.db` a
pelo dejaría afuera lo que todavía está en el write-ahead log — en la migración a
Oracle eran 4,4 MB de `catalog.db`. Deliberadamente NO usa `op_guards.guard_write`:
ese aborta cuando el server está vivo, que es justo cuando este script tiene que
correr (lo dispara un timer diario).

EL BUNDLE NO ESTA CIFRADO. Lleva el `jwt_secret`, las credenciales BYMA y los hashes
de contraseña. Si va a un bucket, ciframos antes:
    age -r <clave-publica> -o bundle.tar.gz.age bundle.tar.gz
La clave privada NO vive en el servidor: la caja puede cifrar y no descifrar.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_BASES = ("catalog.db", "price_history.db", "fci_history.db",
          "ratings_history.db", "index_history.db")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        return "?"


def armar(db_dir: Path, base_dir: Path, destino: Path, incluir_env: bool = True) -> Path:
    """Arma el bundle y devuelve la ruta del .tar.gz."""
    from core.infrastructure.db.backup import _online_copy

    sello = datetime.now().strftime("%Y%m%dT%H%M%S")
    stage = destino / f"stage-{sello}"
    stage.mkdir(parents=True, exist_ok=True)
    manifiesto = {"generado": datetime.now().isoformat(timespec="seconds"),
                  "commit": _commit(), "db_dir": str(db_dir), "archivos": {}}
    try:
        for nombre in _BASES:
            origen = db_dir / nombre
            if not origen.is_file():
                continue
            copia = stage / nombre
            _online_copy(origen, copia)          # WAL-safe, con la app viva
            # Verificar acá y no al restaurar: un backup que no abre es peor que no
            # tenerlo, porque uno cree que está cubierto.
            con = sqlite3.connect("file:%s?mode=ro" % copia.as_posix(), uri=True)
            try:
                ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                con.close()
            if ok != "ok":
                raise RuntimeError(f"{nombre}: la copia no pasa integrity_check ({ok})")
            # Sacar los sidecars que deja abrir la copia. Un `.db` restaurado JUNTO a un
            # `-wal` viejo hace que SQLite intente reproducir ese log sobre una base que
            # ya lo tiene aplicado: `restore_db` del repo los borra por exactamente esta
            # razon. Un bundle que los lleva adentro le pasa el problema al que restaura.
            for sufijo in ("-wal", "-shm"):
                sidecar = copia.with_name(copia.name + sufijo)
                if sidecar.exists():
                    sidecar.unlink()
            manifiesto["archivos"][nombre] = {"sha256": _sha256(copia),
                                              "bytes": copia.stat().st_size,
                                              "integrity": ok}

        sueltos = [db_dir / "jwt_secret", db_dir / "dashboard_layout.json",
                   db_dir / "cartera.json"]
        if incluir_env:
            sueltos.append(base_dir / ".env")
        for origen in sueltos:
            if origen.is_file():
                shutil.copy2(origen, stage / origen.name)
                manifiesto["archivos"][origen.name] = {
                    "sha256": _sha256(stage / origen.name),
                    "bytes": origen.stat().st_size}

        hist = db_dir / "history"
        if hist.is_dir():
            shutil.copytree(hist, stage / "history", dirs_exist_ok=True)
            manifiesto["archivos"]["history/"] = {
                "archivos": len(list((stage / "history").glob("*")))}

        (stage / "MANIFEST.json").write_text(
            json.dumps(manifiesto, indent=2, sort_keys=True), encoding="utf-8")

        # Escribir a un nombre que `rotar` NO mira y renombrar al final. Escribir
        # directo sobre el nombre definitivo dejaba, ante cualquier corte a mitad del
        # tar (disco lleno, OOM, un stop del timer), un .tar.gz PARCIAL con nombre
        # valido: ocupaba un slot de retencion y desalojaba un backup completo. Y como
        # el gzip queda cerrado, el parcial ABRE bien — "se puede abrir?" no lo
        # distingue de uno sano. `os.replace` es atomico dentro del mismo filesystem.
        bundle = destino / f"monitor-{sello}.tar.gz"
        parcial = destino / f"monitor-{sello}.tar.gz.parcial"
        try:
            with tarfile.open(parcial, "w:gz") as tar:
                for item in sorted(stage.iterdir()):
                    tar.add(item, arcname=item.name)
            os.replace(parcial, bundle)
        finally:
            parcial.unlink(missing_ok=True)
        return bundle
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def rotar(destino: Path, keep: int) -> int:
    """Deja los `keep` bundles más nuevos. Devuelve cuántos borró."""
    if keep <= 0:
        return 0
    # Barrer restos de corridas cortadas antes de contar: un `.parcial` huerfano no
    # es un backup y no puede ocupar un slot de retencion.
    for resto in destino.glob("monitor-*.tar.gz.parcial"):
        resto.unlink(missing_ok=True)
    bundles = sorted(destino.glob("monitor-*.tar.gz"), reverse=True)
    borrados = 0
    for viejo in bundles[keep:]:
        try:
            viejo.unlink()
            borrados += 1
        except OSError:
            pass
    return borrados


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Bundle del estado del servidor.")
    ap.add_argument("--out", help="directorio destino (default: db_dir/backups/offsite)")
    ap.add_argument("--keep", type=int, default=3, help="bundles a conservar (default 3)")
    ap.add_argument("--no-env", action="store_true", help="no incluir el .env")
    args = ap.parse_args(argv)

    from config.settings import settings

    db_dir = Path(settings.db_dir)
    # Igual que el backfill: decir contra QUE base corre, antes que nada. En el
    # servidor `MONITOR_DB_DIR` vive en el drop-in de systemd y una shell no lo hereda.
    print("db_dir: %s" % db_dir)
    if not (db_dir / "catalog.db").is_file():
        print("ABORTADO: no hay catalog.db en ese directorio.")
        print("  Casi seguro es el db_dir equivocado. En el servidor:")
        print("      MONITOR_DB_DIR=/var/lib/monitor venv/bin/python scripts/backup_bundle.py")
        return 4

    destino = Path(args.out) if args.out else Path(settings.backup_dir) / "offsite"
    destino.mkdir(parents=True, exist_ok=True)
    bundle = armar(db_dir, Path(settings.base_dir), destino, incluir_env=not args.no_env)
    print("bundle: %s (%.1f MB)" % (bundle, bundle.stat().st_size / 1e6))
    n = rotar(destino, args.keep)
    if n:
        print("rotados: %d bundle(s) viejos borrados (keep=%d)" % (n, args.keep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
