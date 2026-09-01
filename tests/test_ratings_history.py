"""Store del historial de calificaciones FIX (`ratings_history.py`): snapshot diario +
diff up/down/watch contra el corte anterior.

Lo que se protege acá es la CONFIANZA del badge del panel: un cambio inventado (por un
scrape parcial, o por una entidad que apareció/desapareció del listado) es peor que no
avisar nada. Por eso los tests cubren tanto el diff feliz como los tres callarse:
entidad nueva, entidad desaparecida y corte parcial (guard del 60%).
"""

import os
from datetime import date

import pytest

from pathlib import Path

from config.settings import settings
from core.infrastructure.ratings_history import (
    ORDEN_ESCALA,
    RatingsHistoryStore,
    rank_rating,
)


@pytest.fixture
def store(tmp_path):
    return RatingsHistoryStore(os.path.join(str(tmp_path), "rh.db"))


def _row(rating, perspectiva="Estable", area="Finanzas Corporativas", sector="Energía"):
    return {"rating": rating, "perspectiva": perspectiva, "area": area, "sector": sector}


# --------------------------------------------------------------------------- #
# Escala nacional
# --------------------------------------------------------------------------- #
def test_orden_escala_va_de_aaa_a_d():
    assert ORDEN_ESCALA[0] == "AAA"
    assert ORDEN_ESCALA[-1] == "D"
    assert rank_rating("AAA(arg)") < rank_rating("AA+(arg)") < rank_rating("AA(arg)")
    assert rank_rating("AA-(arg)") < rank_rating("A+(arg)")
    assert rank_rating("BBB-(arg)") < rank_rating("BB+(arg)")
    assert rank_rating("CCC(arg)") < rank_rating("CC(arg)") < rank_rating("C(arg)") < rank_rating("D(arg)")


def test_rank_rating_tolera_sufijo_y_mayusculas():
    assert rank_rating("aa-(arg)") == rank_rating("AA-(arg)") == rank_rating("AA-") == rank_rating(" AA- (arg) ")


def test_rank_rating_desconocido_es_none():
    assert rank_rating("") is None
    assert rank_rating(None) is None
    assert rank_rating("N.C") is None
    assert rank_rating("AAAsf(arg)") is None      # calificación de emisión, no de emisor


# --------------------------------------------------------------------------- #
# Snapshot: primer corte, idempotencia, lecturas
# --------------------------------------------------------------------------- #
def test_primer_corte_no_genera_cambios(store):
    res = store.record_corte({"Alpha S.A.": _row("AA(arg)")}, date(2026, 8, 20))
    assert res["status"] == "ok"
    assert res["rows"] == 1
    assert res["changes"] == 0
    assert store.latest_fecha() == "2026-08-20"
    assert store.recent_changes(days=7, hoy=date(2026, 8, 20)) == {}


def test_latest_entries_devuelve_el_ultimo_corte(store):
    store.record_corte({"Alpha S.A.": _row("AA(arg)")}, date(2026, 8, 20))
    store.record_corte({"Alpha S.A.": _row("AA(arg)"), "Beta S.A.": _row("A(arg)")},
                       date(2026, 8, 21))
    ents = {e["entidad"]: e for e in store.latest_entries()}
    assert set(ents) == {"Alpha S.A.", "Beta S.A."}
    assert ents["Beta S.A."]["rating"] == "A(arg)"
    assert ents["Beta S.A."]["sector"] == "Energía"
    assert ents["Beta S.A."]["fecha_corte"] == "2026-08-21"


def test_corte_diario_es_idempotente(store):
    store.record_corte({"Alpha S.A.": _row("AA(arg)")}, date(2026, 8, 20))
    res = store.record_corte({"Alpha S.A.": _row("D(arg)")}, date(2026, 8, 20))  # mismo día
    assert res["status"] == "noop"
    assert res["changes"] == 0
    assert store.latest_entries()[0]["rating"] == "AA(arg)"   # no pisó
    assert store.recent_changes(hoy=date(2026, 8, 20)) == {}


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #
def test_upgrade_y_downgrade_por_la_escala(store):
    store.record_corte({"Alpha S.A.": _row("A+(arg)"), "Beta S.A.": _row("AA(arg)")},
                       date(2026, 8, 20))
    res = store.record_corte({"Alpha S.A.": _row("AA-(arg)"), "Beta S.A.": _row("BBB(arg)")},
                             date(2026, 8, 21))
    assert res["changes"] == 2
    chg = store.recent_changes(hoy=date(2026, 8, 21))
    assert chg["Alpha S.A."]["dir"] == "up"
    assert chg["Alpha S.A."]["from"] == "A+(arg)"
    assert chg["Alpha S.A."]["to"] == "AA-(arg)"
    assert chg["Alpha S.A."]["fecha"] == "2026-08-21"
    assert chg["Beta S.A."]["dir"] == "down"


def test_solo_perspectiva_es_watch(store):
    store.record_corte({"Alpha S.A.": _row("AA(arg)", "Estable")}, date(2026, 8, 20))
    store.record_corte({"Alpha S.A.": _row("AA(arg)", "RW Negativo")}, date(2026, 8, 21))
    chg = store.recent_changes(hoy=date(2026, 8, 21))["Alpha S.A."]
    assert chg["dir"] == "watch"
    assert (chg["persp_from"], chg["persp_to"]) == ("Estable", "RW Negativo")
    assert chg["from"] == chg["to"] == "AA(arg)"


def test_rating_desconocido_cambia_sin_direccion(store):
    store.record_corte({"Alpha S.A.": _row("AA(arg)")}, date(2026, 8, 20))
    store.record_corte({"Alpha S.A.": _row("E(arg)")}, date(2026, 8, 21))
    assert store.recent_changes(hoy=date(2026, 8, 21))["Alpha S.A."]["dir"] == "watch"


def test_sin_cambios_no_inserta_nada(store):
    store.record_corte({"Alpha S.A.": _row("AA(arg)")}, date(2026, 8, 20))
    res = store.record_corte({"Alpha S.A.": _row("AA(arg)")}, date(2026, 8, 21))
    assert res["changes"] == 0
    assert store.recent_changes(hoy=date(2026, 8, 21)) == {}


def test_entidad_nueva_no_es_cambio(store):
    store.record_corte({"Alpha S.A.": _row("AA(arg)")}, date(2026, 8, 20))
    res = store.record_corte({"Alpha S.A.": _row("AA(arg)"), "Beta S.A.": _row("A(arg)")},
                             date(2026, 8, 21))
    assert res["changes"] == 0
    assert store.recent_changes(hoy=date(2026, 8, 21)) == {}


def test_entidad_desaparecida_no_es_cambio(store):
    base = {f"E{i} S.A.": _row("AA(arg)") for i in range(10)}
    store.record_corte(base, date(2026, 8, 20))
    menos = {k: v for k, v in base.items() if k != "E0 S.A."}      # 9/10 = 90% → pasa el guard
    res = store.record_corte(menos, date(2026, 8, 21))
    assert res["status"] == "ok"
    assert res["changes"] == 0
    assert store.recent_changes(hoy=date(2026, 8, 21)) == {}


# --------------------------------------------------------------------------- #
# Guard de sanidad (scrape parcial)
# --------------------------------------------------------------------------- #
def test_guard_descarta_corte_parcial(store):
    base = {f"E{i} S.A.": _row("AA(arg)") for i in range(10)}
    store.record_corte(base, date(2026, 8, 20))
    parcial = {"E0 S.A.": _row("D(arg)"), "E1 S.A.": _row("D(arg)")}   # 2/10 = 20%
    res = store.record_corte(parcial, date(2026, 8, 21))
    assert res["status"] == "discarded"
    assert res["reason"]
    assert res["changes"] == 0
    assert store.latest_fecha() == "2026-08-20"                        # no grabó snapshot
    assert store.recent_changes(hoy=date(2026, 8, 21)) == {}           # ni falsos downgrades


def test_guard_acepta_exactamente_el_umbral(store):
    base = {f"E{i} S.A.": _row("AA(arg)") for i in range(10)}
    store.record_corte(base, date(2026, 8, 20))
    seis = {k: v for k, v in base.items() if k in {f"E{i} S.A." for i in range(6)}}  # 60%
    assert store.record_corte(seis, date(2026, 8, 21))["status"] == "ok"


def test_guard_no_aplica_sin_corte_previo(store):
    assert store.record_corte({"Alpha S.A.": _row("AA(arg)")}, date(2026, 8, 20))["status"] == "ok"


def test_corte_vacio_se_descarta(store):
    assert store.record_corte({}, date(2026, 8, 20))["status"] == "discarded"
    assert store.latest_fecha() is None


# --------------------------------------------------------------------------- #
# Ventana de recent_changes
# --------------------------------------------------------------------------- #
def test_recent_changes_respeta_la_ventana(store):
    store.record_corte({"Alpha S.A.": _row("AA(arg)")}, date(2026, 8, 1))
    store.record_corte({"Alpha S.A.": _row("A(arg)")}, date(2026, 8, 2))       # down, día -6
    store.record_corte({"Beta S.A.": _row("AA(arg)"), "Alpha S.A.": _row("A(arg)")},
                       date(2026, 8, 3))
    store.record_corte({"Beta S.A.": _row("AAA(arg)"), "Alpha S.A.": _row("A(arg)")},
                       date(2026, 8, 4))                                       # up, día -4
    hoy = date(2026, 8, 8)
    assert set(store.recent_changes(days=7, hoy=hoy)) == {"Alpha S.A.", "Beta S.A."}
    assert set(store.recent_changes(days=5, hoy=hoy)) == {"Beta S.A."}          # Alpha ya venció
    assert store.recent_changes(days=3, hoy=hoy) == {}


def test_recent_changes_se_queda_con_el_cambio_mas_reciente(store):
    store.record_corte({"Alpha S.A.": _row("AA(arg)")}, date(2026, 8, 20))
    store.record_corte({"Alpha S.A.": _row("A(arg)")}, date(2026, 8, 21))       # down
    store.record_corte({"Alpha S.A.": _row("AAA(arg)")}, date(2026, 8, 22))     # up
    chg = store.recent_changes(hoy=date(2026, 8, 22))["Alpha S.A."]
    assert (chg["dir"], chg["from"], chg["to"]) == ("up", "A(arg)", "AAA(arg)")


def test_recent_changes_sin_hoy_usa_la_fecha_de_hoy(store):
    assert store.recent_changes() == {}     # store vacío, no explota sin `hoy`


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_settings_declara_la_db_del_historial_fuera_del_working_tree():
    """El invariante es que la .db NO caiga dentro del arbol de git, no que este en
    un directorio puntual: `conftest` la redirige a temp igual que a las otras cuatro
    (catalog/price/fci/index) para que la suite no toque la base real del usuario.
    Comparar contra `settings.db_dir` fijaba justamente el valor que el override pisa."""
    p = settings.ratings_history_db
    assert p.name == "ratings_history.db"
    repo_root = Path(__file__).resolve().parent.parent
    assert repo_root not in p.resolve().parents, f"{p} quedaria dentro del working tree"


def test_el_default_de_la_db_cuelga_del_db_dir():
    """Sin override de entorno, la DB vive al lado de las otras (db_dir). Se mira la
    DECLARACION del campo, porque la instancia viva ya trae el override de conftest."""
    from config.settings import Settings
    assert Settings().ratings_history_db.name == "ratings_history.db"


# --------------------------------------------------------------------------- #
# Deriva del guard y huecos del histórico (hallazgos de la revisión adversarial)
# --------------------------------------------------------------------------- #

def test_el_guard_no_se_ratchetea_con_cortes_degradados(store):
    """El guard tiene que medir contra el TAMAÑO SANO del histórico, no contra el
    corte anterior a secas.

    Si la referencia es solo el corte previo, una degradación gradual pasa entera:
    100 -> 62 (62%, pasa) -> 40 (65% de 62, pasa). Cada corte flaco se vuelve la nueva
    línea de base y el siguiente se mide contra ella, así que el scrape se puede ir
    desangrando sin que salte nunca el guard. Contra el máximo histórico, 40 es el 40%
    de 100 y se descarta."""
    cien = {f"E{i:03d}": _row("A(arg)") for i in range(100)}
    assert store.record_corte(cien, date(2026, 1, 1))["status"] == "ok"
    sesenta_dos = {f"E{i:03d}": _row("A(arg)") for i in range(62)}
    assert store.record_corte(sesenta_dos, date(2026, 1, 2))["status"] == "ok"
    cuarenta = {f"E{i:03d}": _row("A(arg)") for i in range(40)}
    r = store.record_corte(cuarenta, date(2026, 1, 3))
    assert r["status"] == "discarded", "el corte degradado se colo por comparar solo contra el previo"
    assert "40" in r["reason"]


def test_un_hueco_de_un_corte_no_se_traga_el_cambio(store):
    """Se diffea contra el ULTIMO ESTADO CONOCIDO de cada entidad, no contra el corte
    anterior a secas.

    Si una entidad falta en UN corte (hueco del scrape, o FIX que no la publica ese
    día) y vuelve con otra calificación, comparar solo contra el corte previo la ve
    como 'nueva' y se calla el cambio para siempre: justo el downgrade que el operador
    necesita ver."""
    base = {f"E{i:03d}": _row("A(arg)") for i in range(20)}
    con_x = dict(base, X=_row("A(arg)"))
    assert store.record_corte(con_x, date(2026, 2, 1))["status"] == "ok"
    # X no viene en este corte (el resto sí, así que el guard no se queja).
    assert store.record_corte(base, date(2026, 2, 2))["status"] == "ok"
    # X vuelve, mejorada.
    r = store.record_corte(dict(base, X=_row("AAA(arg)")), date(2026, 2, 3))
    assert r["status"] == "ok"
    ch = store.recent_changes(days=7, hoy=date(2026, 2, 3))
    assert "X" in ch, "se perdio el cambio de X por el hueco del corte intermedio"
    assert ch["X"]["dir"] == "up"
    assert ch["X"]["from"] == "A(arg)" and ch["X"]["to"] == "AAA(arg)"


def test_entidad_nunca_vista_sigue_sin_generar_cambio(store):
    """El contrapeso del test anterior: 'ultimo estado conocido' no puede convertir a
    una entidad NUEVA en un cambio inventado."""
    base = {f"E{i:03d}": _row("A(arg)") for i in range(20)}
    store.record_corte(base, date(2026, 3, 1))
    store.record_corte(dict(base, NUEVA=_row("BB(arg)")), date(2026, 3, 2))
    assert "NUEVA" not in store.recent_changes(days=7, hoy=date(2026, 3, 2))
