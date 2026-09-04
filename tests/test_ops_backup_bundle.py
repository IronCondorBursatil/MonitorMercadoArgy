"""El bundle que saca el estado de la caja.

`backup_db` respalda `catalog.db` y sólo eso, al MISMO disco. Afuera quedaban los
cuatro históricos —que se acumulan rueda a rueda y **no se backfillean**—, el
`jwt_secret`, el `.env` con las credenciales BYMA y las tenencias del usuario. Con una
instancia que Oracle puede reclamar por inactividad, eso no es teórico.
"""

import json
import sqlite3
import tarfile
from datetime import date
from pathlib import Path

import pytest


@pytest.fixture
def db_dir(tmp_path):
    """Un `db_dir` con las 5 bases y los sueltos, como el del servidor."""
    d = tmp_path / "db"
    d.mkdir()
    from core.infrastructure.fci_history import FCIHistoryStore

    con = sqlite3.connect(d / "catalog.db")
    con.execute("CREATE TABLE instruments (ticker TEXT PRIMARY KEY, nombre TEXT)")
    con.executemany("INSERT INTO instruments VALUES (?,?)",
                    [("AL30", "Bonar 30"), ("GD30", "Global 30")])
    con.commit()
    con.close()

    FCIHistoryStore(d / "fci_history.db").record_snapshot(
        [{"fondo": "F", "vcp": 1.0, "ccp": 1.0, "patrimonio": 1.0}], date(2026, 9, 1))
    for otra in ("price_history.db", "ratings_history.db", "index_history.db"):
        c = sqlite3.connect(d / otra)
        c.execute("CREATE TABLE t (x INTEGER)")
        c.execute("INSERT INTO t VALUES (1)")
        c.commit()
        c.close()

    (d / "jwt_secret").write_text("un-secreto", encoding="utf-8")
    (d / "cartera.json").write_text('{"holdings": [{"ticker": "AL30"}]}', encoding="utf-8")
    (d / "dashboard_layout.json").write_text("{}", encoding="utf-8")
    (d / "history").mkdir()
    (d / "history" / "cer_diario.csv").write_text("fecha,valor\n", encoding="utf-8")
    return d


@pytest.fixture
def base_dir(tmp_path):
    b = tmp_path / "repo"
    b.mkdir()
    (b / ".env").write_text("BYMADATA_USER=x\nBYMADATA_PASS=y\n", encoding="utf-8")
    return b


def _contenido(bundle: Path) -> set:
    with tarfile.open(bundle) as tar:
        return {m.name for m in tar.getmembers()}


def test_el_bundle_lleva_TODO_lo_que_no_se_puede_reconstruir(db_dir, base_dir, tmp_path):
    """La lista no es cosmética: cada archivo que falte es algo que, si se pierde la
    caja, no vuelve. Los históricos no se backfillean; sin `jwt_secret` caen todas las
    sesiones; sin `.env` hay que volver a cargar las credenciales BYMA a mano; y
    `cartera.json` son datos que el usuario cargó uno por uno."""
    from scripts.backup_bundle import armar

    bundle = armar(db_dir, base_dir, tmp_path / "out")
    dentro = _contenido(bundle)
    for esperado in ("catalog.db", "price_history.db", "fci_history.db",
                     "ratings_history.db", "index_history.db",
                     "jwt_secret", "cartera.json", "dashboard_layout.json",
                     ".env", "MANIFEST.json"):
        assert esperado in dentro, f"el bundle no lleva {esperado}"
    assert any(n.startswith("history") for n in dentro), "faltan los CSV de series"


def test_las_bases_del_bundle_ABREN_y_tienen_los_datos(db_dir, base_dir, tmp_path):
    """Un backup que no se puede abrir es peor que no tenerlo: uno cree que está
    cubierto. Se verifica al ARMARLO, no al restaurar."""
    from scripts.backup_bundle import armar

    bundle = armar(db_dir, base_dir, tmp_path / "out")
    destino = tmp_path / "restaurado"
    with tarfile.open(bundle) as tar:
        tar.extractall(destino, filter="data")

    con = sqlite3.connect(destino / "catalog.db")
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT COUNT(*) FROM instruments").fetchone()[0] == 2
    con.close()


def test_el_manifiesto_permite_verificar_la_copia(db_dir, base_dir, tmp_path):
    """Sin sha256 por archivo no hay forma de saber si el bundle llegó entero al otro
    lado — que es exactamente el momento en que uno lo necesita."""
    from scripts.backup_bundle import armar

    bundle = armar(db_dir, base_dir, tmp_path / "out")
    destino = tmp_path / "r"
    with tarfile.open(bundle) as tar:
        tar.extractall(destino, filter="data")
    man = json.loads((destino / "MANIFEST.json").read_text(encoding="utf-8"))
    assert man["archivos"]["catalog.db"]["integrity"] == "ok"
    assert len(man["archivos"]["catalog.db"]["sha256"]) == 64
    assert "commit" in man and "generado" in man


def test_funciona_con_un_escritor_vivo(db_dir, base_dir, tmp_path):
    """El timer corre a diario con la app arriba. Copiar el `.db` a pelo dejaría afuera
    lo que está en el WAL — en la migración a Oracle eran 4,4 MB de catalog.db."""
    from scripts.backup_bundle import armar

    con = sqlite3.connect(db_dir / "catalog.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("INSERT INTO instruments VALUES ('TX26', 'Boncer 26')")
    con.commit()                       # commiteado pero todavía en el WAL
    try:
        bundle = armar(db_dir, base_dir, tmp_path / "out")
        destino = tmp_path / "r"
        with tarfile.open(bundle) as tar:
            tar.extractall(destino, filter="data")
        c2 = sqlite3.connect(destino / "catalog.db")
        assert c2.execute(
            "SELECT COUNT(*) FROM instruments").fetchone()[0] == 3, (
            "la copia perdió lo que estaba en el write-ahead log")
        c2.close()
    finally:
        con.close()


def test_se_puede_excluir_el_env(db_dir, base_dir, tmp_path):
    """Si el bundle va a un destino sin cifrar, el `.env` es lo primero que uno quiere
    dejar afuera."""
    from scripts.backup_bundle import armar

    bundle = armar(db_dir, base_dir, tmp_path / "out", incluir_env=False)
    assert ".env" not in _contenido(bundle)


def test_la_rotacion_conserva_los_mas_nuevos(tmp_path):
    from scripts.backup_bundle import rotar

    d = tmp_path / "out"
    d.mkdir()
    for sello in ("20260101T000000", "20260102T000000", "20260103T000000",
                  "20260104T000000"):
        (d / f"monitor-{sello}.tar.gz").write_text("x", encoding="utf-8")
    assert rotar(d, 2) == 2
    quedan = sorted(p.name for p in d.glob("monitor-*.tar.gz"))
    assert quedan == ["monitor-20260103T000000.tar.gz", "monitor-20260104T000000.tar.gz"]


def test_la_huella_detecta_una_fila_de_diferencia(db_dir):
    """`db_fingerprint` es lo que convierte "el backup pesa lo que tiene que pesar" en
    "el backup tiene los mismos datos"."""
    from scripts.db_fingerprint import huella_de

    antes = huella_de(db_dir / "catalog.db")
    con = sqlite3.connect(db_dir / "catalog.db")
    con.execute("INSERT INTO instruments VALUES ('AE38', 'x')")
    con.commit()
    con.close()
    despues = huella_de(db_dir / "catalog.db")
    assert antes["instruments"]["filas"] == 2
    assert despues["instruments"]["filas"] == 3
    assert antes["instruments"]["sha256"] != despues["instruments"]["sha256"]


def test_la_huella_no_depende_del_orden_fisico(tmp_path):
    """Dos copias con las mismas filas en distinto orden físico —lo que produce un
    VACUUM o un backup online— tienen que dar la MISMA huella; si no, todo diff de
    backup daría falso positivo."""
    from scripts.db_fingerprint import huella_de

    a, b = tmp_path / "a.db", tmp_path / "b.db"
    for ruta, orden in ((a, [(1, "x"), (2, "y")]), (b, [(2, "y"), (1, "x")])):
        con = sqlite3.connect(ruta)
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        con.executemany("INSERT INTO t VALUES (?,?)", orden)
        con.commit()
        con.close()
    assert huella_de(a)["t"]["sha256"] == huella_de(b)["t"]["sha256"]


def test_el_bundle_no_lleva_sidecars_de_wal(db_dir, base_dir, tmp_path):
    """Restaurar un `.db` junto a un `-wal` viejo hace que SQLite intente reproducir
    ese log sobre una base que ya lo tiene aplicado — por eso `restore_db` los borra.
    Un bundle que los lleva adentro le pasa ese problema al que restaura, justo en el
    momento en que menos margen tiene.

    Apareció al correr el script contra la base viva del servidor: el propio
    `integrity_check` de la copia deja los sidecars."""
    from scripts.backup_bundle import armar

    con = sqlite3.connect(db_dir / "catalog.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("INSERT INTO instruments VALUES ('AE38', 'x')")
    con.commit()
    try:
        dentro = _contenido(armar(db_dir, base_dir, tmp_path / "out"))
    finally:
        con.close()
    sidecars = [n for n in dentro if n.endswith(("-wal", "-shm"))]
    assert not sidecars, f"el bundle lleva sidecars de WAL: {sidecars}"


def test_un_bundle_cortado_a_la_mitad_NO_queda_con_nombre_valido(db_dir, base_dir,
                                                                 tmp_path, monkeypatch):
    """Escribir directo sobre `monitor-<sello>.tar.gz` dejaba, ante cualquier corte a
    mitad del tar (disco lleno, OOM, un stop del timer), un archivo PARCIAL con nombre
    valido. `rotar` sólo mira el glob y ordena por nombre: ese parcial ocupaba un slot
    de retención y desalojaba un backup completo. Peor, el gzip queda cerrado, así que
    el parcial ABRE bien — un chequeo de "¿se puede abrir?" no lo distingue de uno sano.

    Hallazgo de la auditoría 2026-09-04 (severidad alta)."""
    from scripts import backup_bundle

    destino = tmp_path / "out"
    destino.mkdir()
    completo = destino / "monitor-20260101T000000.tar.gz"
    completo.write_text("un backup sano", encoding="utf-8")

    real = backup_bundle.tarfile.open

    class _CortaAlSegundo:
        """Un tar que escribe la primera entrada y revienta en la segunda."""

        def __init__(self, *a, **kw):
            self._tar = real(*a, **kw)
            self._n = 0

        def add(self, *a, **kw):
            self._n += 1
            if self._n > 1:
                raise OSError(28, "No space left on device")
            return self._tar.add(*a, **kw)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._tar.__exit__(*exc)

    monkeypatch.setattr(backup_bundle.tarfile, "open", _CortaAlSegundo)
    with pytest.raises(OSError):
        backup_bundle.armar(db_dir, base_dir, destino)

    quedan = sorted(p.name for p in destino.glob("monitor-*"))
    assert quedan == ["monitor-20260101T000000.tar.gz"], (
        f"un bundle cortado sobrevivió con nombre de backup válido: {quedan}")

    # Y la rotación no lo cuenta ni deja restos.
    assert backup_bundle.rotar(destino, 1) == 0
    assert completo.is_file(), "la rotación se llevó puesto el backup sano"
