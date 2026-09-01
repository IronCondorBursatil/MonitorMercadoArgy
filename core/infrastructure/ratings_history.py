"""Store local del historial de calificaciones FIX SCR: un **snapshot por día** del
listado público + los **cambios** que ese corte reveló contra el corte anterior.

Por qué existe: FIX publica el listado VIGENTE, no el histórico de acciones sobre cada
emisor. El único modo de saber que Alpha pasó de A+(arg) a AA-(arg) es haber guardado el
corte de ayer. De ahí las dos tablas: `fix_snapshot` (la foto diaria, que además le da al
panel su `as_of` real) y `fix_changes` (el diff materializado, que alimenta el badge de
7 días en la fila del emisor).

Tres decisiones que valen más que el código:

1. **El diff sólo habla de entidades presentes en AMBOS cortes.** Una entidad nueva no
   tiene contra qué compararse; una que desapareció puede ser un retiro de calificación
   o un hueco del scrape — inventar un downgrade ahí sería peor que callarse.
2. **Guard de sanidad**: un corte con menos del 60% de las filas del último bueno se
   descarta ENTERO (ni snapshot ni cambios). Un scrape parcial —timeout a mitad de la
   paginación— generaría decenas de falsos cambios y quemaría la confianza del badge.
   Mejor un día sin corte.
3. **La ventana de `recent_changes` se mide contra `fix_changes.fecha`** (el corte en el
   que LO DETECTAMOS), no contra la fecha que declara FIX: esa suele ser anterior al
   primer corte de la historia y el badge no se vería nunca.

Persistencia: SQLite en `%LOCALAPPDATA%\\monitor` (fuera del working tree de git — ver
invariante en CLAUDE.md). Mismo patrón que `fci_history.py`: conexión efímera por
operación, `RLock` para el cache en memoria, path inyectable para los tests.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from contextlib import closing
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from config.settings import settings

logger = logging.getLogger(__name__)

# Escala nacional argentina, de mejor a peor, con notches. El índice ES el orden: menor
# = mejor crédito. Vive acá (y no en ratings.py) porque su único consumidor es el diff:
# ratings.py sólo necesita la LETRA base para colorear el badge (`_grade`).
ORDEN_ESCALA: Tuple[str, ...] = (
    "AAA",
    "AA+", "AA", "AA-",
    "A+", "A", "A-",
    "BBB+", "BBB", "BBB-",
    "BB+", "BB", "BB-",
    "B+", "B", "B-",
    "CCC+", "CCC", "CCC-",
    "CC", "C", "D",
)
_RANK: Dict[str, int] = {r: i for i, r in enumerate(ORDEN_ESCALA)}

# Fracción mínima de filas para aceptar un corte nuevo (ver punto 2). La referencia NO
# es el corte anterior sino el MÁS GRANDE de los últimos `_GUARD_VENTANA` cortes: contra
# el previo a secas el umbral se ratchetea (100 → 62 → 40 pasa entero, porque cada corte
# flaco se vuelve la nueva línea de base). La ventana deja que el universo se achique de
# a poco por razones legítimas sin que el guard quede clavado en un máximo viejo.
_GUARD_MIN_RATIO = 0.60
_GUARD_VENTANA = 30


def rank_rating(rating: Optional[str]) -> Optional[int]:
    """Posición en la escala nacional (0 = AAA, mayor = peor), o None si no se reconoce.

    Tolera el sufijo `(arg)`, mayúsculas/minúsculas y espacios sueltos. Devuelve None —en
    vez de adivinar— para todo lo que no sea un escalón de emisor: `N.C`, `E(arg)`, o los
    ratings de emisión (`AAAsf(arg)`). Un cambio hacia/desde un rating sin rank se
    clasifica **sin dirección** (`watch`), que es lo honesto.
    """
    if not rating:
        return None
    base = str(rating).upper().split("(")[0]          # "AA-(arg)" → "AA-"
    return _RANK.get(re.sub(r"\s+", "", base))


def _iso(d) -> str:
    """Fecha → 'YYYY-MM-DD'. Acepta date o el string ya formateado (viene del loop)."""
    return d.isoformat() if isinstance(d, date) else str(d).strip()[:10]


class RatingsHistoryStore:
    """`fix_snapshot` (foto diaria) + `fix_changes` (diff) sobre SQLite.

    Thread-safe igual que los otros stores: conexión efímera por operación y un `RLock`
    que serializa el write-path (el loop diario) contra el read-path del panel, además de
    proteger el cache del último corte (lo lee cada render de fila de /on).
    """

    def __init__(self, db_path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        self._latest: Optional[Tuple[Optional[str], List[dict]]] = None

    # ----------------------------------------------------------------- schema
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS fix_snapshot ("
            "  fecha_corte TEXT NOT NULL,"          # ISO 'YYYY-MM-DD'
            "  entidad     TEXT NOT NULL,"
            "  area        TEXT,"
            "  sector      TEXT,"
            "  rating      TEXT,"                   # largo plazo, escala nacional
            "  perspectiva TEXT,"
            "  PRIMARY KEY (fecha_corte, entidad))"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS fix_changes ("
            "  fecha       TEXT NOT NULL,"          # corte en que LO DETECTAMOS
            "  entidad     TEXT NOT NULL,"
            "  area        TEXT,"
            "  rating_from TEXT,"
            "  rating_to   TEXT,"
            "  persp_from  TEXT,"
            "  persp_to    TEXT,"
            "  tipo        TEXT,"                   # 'up' | 'down' | 'watch'
            "  PRIMARY KEY (fecha, entidad))"
        )
        con.execute("CREATE INDEX IF NOT EXISTS ix_fix_changes_fecha ON fix_changes(fecha)")
        return con

    # ------------------------------------------------------------- write-path
    def record_corte(self, rows: Dict[str, dict], hoy) -> dict:
        """Graba el corte de `hoy` y devuelve el resumen `{status, fecha, rows, changes, …}`.

        `rows` = `{entidad: {rating, perspectiva, area, sector}}`. **Idempotente por día**:
        si ya hay corte de `hoy` es no-op (restart-safe: el loop puede correr varias veces
        por día sin re-diffear). Si no, valida el guard de sanidad, persiste el snapshot y
        materializa el diff contra el corte anterior más reciente.

        `status ∈ {ok, noop, discarded, error}`; `discarded`/`error` traen `reason`.
        """
        fecha = _iso(hoy)
        clean = _clean_rows(rows)
        try:
            with self._lock, closing(self._connect()) as con:
                ya = con.execute(
                    "SELECT COUNT(*) FROM fix_snapshot WHERE fecha_corte=?", (fecha,)
                ).fetchone()[0]
                if ya:
                    return {"status": "noop", "fecha": fecha, "rows": ya, "changes": 0,
                            "reason": None}

                prev = self._estado_previo(con, fecha)
                ref_n, ref_fecha = self._referencia_guard(con, fecha)
                bad = _guard(clean, ref_n, ref_fecha)
                if bad:
                    logger.warning("ratings_history: corte %s descartado — %s", fecha, bad)
                    return {"status": "discarded", "fecha": fecha, "rows": len(clean),
                            "changes": 0, "reason": bad}

                cambios = _diff(prev, clean) if prev else []
                with con:      # una sola transacción: snapshot y cambios van juntos o no van
                    con.executemany(
                        "INSERT INTO fix_snapshot "
                        "(fecha_corte, entidad, area, sector, rating, perspectiva) "
                        "VALUES (?,?,?,?,?,?)",
                        [(fecha, e, r["area"], r["sector"], r["rating"], r["perspectiva"])
                         for e, r in clean.items()],
                    )
                    con.executemany(
                        "INSERT INTO fix_changes (fecha, entidad, area, rating_from, "
                        "rating_to, persp_from, persp_to, tipo) VALUES (?,?,?,?,?,?,?,?)",
                        [(fecha, *c) for c in cambios],
                    )
                self._latest = None      # el próximo read-path relee del disco
        except sqlite3.Error as e:
            logger.warning("ratings_history: write failed (%s)", e)
            return {"status": "error", "fecha": fecha, "rows": 0, "changes": 0,
                    "reason": str(e)}

        tipos = [c[-1] for c in cambios]
        return {"status": "ok", "fecha": fecha, "rows": len(clean), "changes": len(cambios),
                "up": tipos.count("up"), "down": tipos.count("down"),
                "watch": tipos.count("watch"), "reason": None}

    @staticmethod
    def _estado_previo(con: sqlite3.Connection, fecha: str) -> Dict[str, dict]:
        """ÚLTIMO ESTADO CONOCIDO de cada entidad antes de `fecha` — no el corte anterior.

        Diffear contra el corte previo a secas pierde el cambio de toda entidad que
        falte en UN solo corte: al volver no está en el previo, se la toma por nueva y
        el cambio se calla para siempre. Un hueco (FIX que no la publica un día, o una
        página que no entró) no puede tragarse un downgrade. Como cada entidad se
        compara contra la última vez que SE LA VIO, el hueco deja de importar."""
        return {
            ent: {"area": area, "sector": sector, "rating": rating, "perspectiva": persp}
            for ent, area, sector, rating, persp in con.execute(
                "SELECT s.entidad, s.area, s.sector, s.rating, s.perspectiva "
                "FROM fix_snapshot s JOIN ("
                "  SELECT entidad, MAX(fecha_corte) AS f FROM fix_snapshot "
                "  WHERE fecha_corte < ? GROUP BY entidad"
                ") u ON u.entidad = s.entidad AND u.f = s.fecha_corte", (fecha,))
        }

    @staticmethod
    def _referencia_guard(con: sqlite3.Connection, fecha: str) -> Tuple[int, Optional[str]]:
        """(tamaño, fecha) del corte MÁS GRANDE de la ventana reciente, contra el que se
        mide el corte nuevo. Ver `_GUARD_VENTANA`."""
        row = con.execute(
            "SELECT fecha_corte, COUNT(*) AS c FROM fix_snapshot WHERE fecha_corte < ? "
            "GROUP BY fecha_corte ORDER BY fecha_corte DESC LIMIT ?",
            (fecha, _GUARD_VENTANA)).fetchall()
        if not row:
            return 0, None
        mejor = max(row, key=lambda r: r[1])
        return mejor[1], mejor[0]

    # -------------------------------------------------------------- read-path
    def latest_fecha(self) -> Optional[str]:
        """Fecha ISO del último corte guardado (el `as_of` real del panel), o None."""
        return self._load_latest()[0]

    def latest_entries(self) -> List[dict]:
        """Filas del último corte: `{entidad, emisor, area, sector, rating, perspectiva,
        fecha_corte}`. `emisor` duplica a `entidad` a propósito: es el nombre con el que
        `ratings.py` conoce la columna del CSV, así el merge no tiene que renombrar."""
        return [dict(e) for e in self._load_latest()[1]]

    def _load_latest(self) -> Tuple[Optional[str], List[dict]]:
        with self._lock:
            if self._latest is not None:
                return self._latest
            fecha: Optional[str] = None
            out: List[dict] = []
            try:
                with closing(self._connect()) as con:
                    row = con.execute("SELECT MAX(fecha_corte) FROM fix_snapshot").fetchone()
                    fecha = row[0] if row else None
                    if fecha:
                        out = [
                            {"entidad": ent, "emisor": ent, "area": area, "sector": sector,
                             "rating": rating, "perspectiva": persp, "fecha_corte": fecha}
                            for ent, area, sector, rating, persp in con.execute(
                                "SELECT entidad, area, sector, rating, perspectiva "
                                "FROM fix_snapshot WHERE fecha_corte=? ORDER BY entidad",
                                (fecha,))
                        ]
            except sqlite3.Error as e:
                # No latchear el vacío: un error transitorio de SQLite dejaría el panel sin
                # calificaciones para todo el proceso. La próxima lectura reintenta.
                logger.warning("ratings_history: read failed, will retry (%s)", e)
                return None, []
            self._latest = (fecha, out)
            return self._latest

    def recent_changes(self, days: int = 7, hoy=None) -> Dict[str, dict]:
        """`{entidad: {dir, from, to, persp_from, persp_to, fecha, area}}` de la ventana.

        La ventana son `days` días de calendario CONTANDO hoy (con `days=7`, un cambio
        detectado hace 6 días todavía se ve; uno de hace 7, no). Si una entidad cambió más
        de una vez en la ventana queda el cambio más reciente — es el que describe el
        rating vigente que muestra la fila.
        """
        hoy_d = hoy if isinstance(hoy, date) else (
            date.fromisoformat(_iso(hoy)) if hoy else date.today())
        desde = (hoy_d - timedelta(days=max(int(days), 1) - 1)).isoformat()
        out: Dict[str, dict] = {}
        try:
            with closing(self._connect()) as con:
                for fecha, ent, area, rf, rt, pf, pt, tipo in con.execute(
                        "SELECT fecha, entidad, area, rating_from, rating_to, persp_from, "
                        "persp_to, tipo FROM fix_changes WHERE fecha >= ? ORDER BY fecha",
                        (desde,)):
                    out[ent] = {"dir": tipo, "from": rf, "to": rt, "persp_from": pf,
                                "persp_to": pt, "fecha": fecha, "area": area}
        except sqlite3.Error as e:
            logger.warning("ratings_history: recent_changes failed (%s)", e)
            return {}
        return out


# --------------------------------------------------------------------------- #
# Piezas puras del corte (testeables sin disco)
# --------------------------------------------------------------------------- #
def _clean_rows(rows: Optional[Dict[str, dict]]) -> Dict[str, dict]:
    """Normaliza el corte crudo. Descarta filas sin entidad o **sin rating LP**: no
    aportan al panel y, peor, un rating vacío contra uno anterior se vería como cambio."""
    clean: Dict[str, dict] = {}
    for ent, r in (rows or {}).items():
        entidad = " ".join(str(ent or "").split())
        if not entidad or not isinstance(r, dict):
            continue
        rating = str(r.get("rating") or "").strip()
        if not rating:
            continue
        clean[entidad] = {
            "rating": rating,
            "perspectiva": str(r.get("perspectiva") or "").strip(),
            "area": str(r.get("area") or "").strip(),
            "sector": str(r.get("sector") or "").strip(),
        }
    return clean


def _guard(clean: Dict[str, dict], ref_n: int, ref_fecha: Optional[str]) -> Optional[str]:
    """Motivo por el que el corte se descarta entero, o None si es aceptable.

    `ref_n` es el corte más grande de la ventana reciente (no el anterior): así una
    degradación gradual no se cuela corriendo la línea de base hacia abajo."""
    if not clean:
        return "corte vacío (¿scrape caído o HTML cambiado?)"
    if ref_n and len(clean) < _GUARD_MIN_RATIO * ref_n:
        return (f"corte parcial: {len(clean)} filas < {_GUARD_MIN_RATIO:.0%} de las "
                f"{ref_n} del corte {ref_fecha}")
    return None


def _diff(prev: Dict[str, dict], cur: Dict[str, dict]) -> List[tuple]:
    """Cambios `(entidad, area, rating_from, rating_to, persp_from, persp_to, tipo)`.

    Sólo entidades presentes en AMBOS cortes (ver docstring del módulo). `up`/`down` sale
    del orden de la escala; si alguno de los dos ratings no tiene rank, el cambio existe
    pero sin dirección → `watch`, igual que el cambio de sola perspectiva.
    """
    out: List[tuple] = []
    for ent, new in cur.items():
        old = prev.get(ent)
        if old is None:
            continue
        r_old, r_new = old.get("rating") or "", new["rating"]
        p_old, p_new = old.get("perspectiva") or "", new["perspectiva"]
        if r_old != r_new:
            a, b = rank_rating(r_old), rank_rating(r_new)
            tipo = "watch" if a is None or b is None or a == b else ("up" if b < a else "down")
        elif p_old != p_new:
            tipo = "watch"
        else:
            continue
        out.append((ent, new["area"], r_old, r_new, p_old, p_new, tipo))
    return out


# --------------------------------------------------------------------------- #
# Singleton de proceso: el loop diario escribe y el panel lee del mismo cache.
# Path desde settings (override por env MONITOR_RATINGS_HISTORY_DB en tests/prod).
# --------------------------------------------------------------------------- #
_STORE: Optional[RatingsHistoryStore] = None
_STORE_LOCK = threading.Lock()


def get_ratings_history_store() -> RatingsHistoryStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = RatingsHistoryStore(settings.ratings_history_db)
    return _STORE
