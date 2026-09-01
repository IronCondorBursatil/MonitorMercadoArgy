"""Matcher de calificaciones FIX SCR por emisor (core/infrastructure/ratings.py).

Verifica los casos no triviales (acrónimos, sufijos, "- Clase X") y —clave— que no
haya falsos positivos (TGS no debe matchear TGN; bancos/financieras sin rating).

La segunda mitad cubre el MERGE del corte diario (store `ratings_history`) sobre el CSV
semilla: quién pisa a quién, qué se conserva de cada lado y cuándo se tira el cache."""

from datetime import date

import pytest

from core.infrastructure import ratings
from core.infrastructure.ratings import rating_for


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Store de historial VACÍO y propio de cada test.

    Autouse a propósito: sin esto la suite leería el `ratings_history.db` REAL del usuario
    (el loop diario lo escribe) y las aserciones de más abajo dependerían del corte de hoy.
    Además tira los caches de `ratings` al entrar Y al salir: son de proceso, así que un
    corte de juguete se filtraría a los otros módulos de test."""
    from core.infrastructure import ratings_history as rh
    st = rh.RatingsHistoryStore(tmp_path / "rh.db")
    monkeypatch.setattr(rh, "_STORE", st)
    ratings.invalidate_cache()
    yield st
    ratings.invalidate_cache()


def _fila(rating, perspectiva="Estable", sector="Energia", area="Finanzas Corporativas"):
    """Fila del corte tal como la deja `fix_ratings.mejor_fila_por_entidad` + el loop."""
    return {"rating": rating, "perspectiva": perspectiva, "sector": sector, "area": area}


def test_matches_known_emisores():
    assert rating_for("YPF S.A.")["rating"] == "AAA(arg)"
    assert rating_for("ARCOR S.A.I.C.")["rating"] == "AAA(arg)"
    assert rating_for("ARCOR S.A.I.C. - Clase 1")["rating"] == "AAA(arg)"       # strip "- Clase X"
    assert rating_for("COMPAÑIA GENERAL DE COMBUSTIBLES S.A.")["rating"] == "AA-(arg)"
    assert rating_for("LOMA NEGRA COMPAÑIA INDUSTRIAL ARGENTINA SOCIEDAD ANONIMA")["rating"] == "AAA(arg)"
    # acrónimo entre paréntesis (EDENOR) pese a typos/abreviaturas en el nombre
    assert rating_for("EMPRESA DISTRIB. Y COMERCIALZADORA NORTE S.A. (EDENOR S.A.)")["rating"] == "AA-(arg)"
    assert rating_for("EDESA S.A.")["rating"] == "AA-(arg)"                      # token = acrónimo FIX
    t = rating_for("TELECOM ARGENTINA S. A.")
    assert t["rating"] == "AAA(arg)" and t["perspectiva"] == "Estable"
    assert rating_for("YPF Energía Eléctrica S.A.")["rating"] == "AAA(arg)"      # ≠ YPF S.A.


def test_matches_nombre_comercial_corto():
    """El catálogo trae el nombre COMERCIAL corto y el listado FIX el legal largo:
    la contención tiene que evaluarse en las dos direcciones."""
    irsa = rating_for("IRSA")                       # ⊂ "IRSA Inversiones y Representaciones S.A."
    assert irsa["rating"] == "AAA(arg)" and irsa["emisor"].startswith("IRSA")
    # el alias entre paréntesis también es un nombre buscable
    luz = rating_for("YPF LUZ")                     # = "(YPF Luz)" de "YPF Energía Eléctrica S.A."
    assert luz["rating"] == "AAA(arg)" and "Energía Eléctrica" in luz["emisor"]
    # ex-denominación entre paréntesis: "Tango Energy … (Ex Petrolera Aconcagua Energía S.A.)"
    assert rating_for("PETROLERA ACONCAGUA ENERGIA S.A.")["emisor"].startswith("Tango Energy")


def test_prefiere_el_emisor_mas_especifico():
    """Con varios candidatos que contienen el mismo token gana el más específico,
    y el nombre exacto nunca cae en el alias de otro emisor."""
    assert rating_for("YPF").get("emisor") == "YPF S.A."             # no "YPF Energía Eléctrica"
    assert rating_for("MSU").get("emisor") == "MSU S.A."             # no "MSU Energy"/"MSU Green"
    assert rating_for("MSU ENERGY").get("emisor") == "MSU Energy S.A."


def test_ambiguo_no_devuelve_rating():
    """Un token compartido por varios emisores es ambiguo: sin calificación es mejor
    que la calificación equivocada."""
    assert rating_for("GAS") is None            # Transportadora del Norte / Camuzzi Gas Pampeana
    assert rating_for("ENERGY") is None         # MSU Energy / Vista Energy / MSU Green Energy
    assert rating_for("MOLINOS") is None        # Molinos Agro / Molinos Río de la Plata
    assert rating_for("CENTRAL") is None        # Central Puerto / Central Térmica Roca
    # …pero el nombre exacto sí gana sobre el homónimo más largo
    assert rating_for("HAVANNA")["emisor"] == "Havanna S.A."            # no "Havanna Holding"
    assert rating_for("HAVANNA HOLDING")["emisor"] == "Havanna Holding S.A."


def test_grade_for_color():
    assert rating_for("YPF S.A.")["grade"] == "strong"      # AAA
    assert rating_for("Milicic S.A.")["grade"] == "good"     # A+
    assert rating_for("Roch S.A.")["grade"] == "distress"    # C


def test_no_false_positives():
    # TGS (del Sur) NO debe matchear TGN (del Norte), que sí está en el listado
    assert rating_for("TRANSPORTADORA DE GAS DEL SUR S.A.") is None
    assert rating_for("TGS") is None
    # bancos / financieras / no listados → sin calificación
    for e in ("BANCO MACRO S.A.", "BANCO DE GALICIA Y BUENOS AIRES S.A.", "MIRGOR S.A.",
              "GENNEIA S.A.", "CNH Industrial Capital Argentina S.A.",
              "Mercado Pago Servicios de Procesamiento S.R.L."):
        assert rating_for(e) is None, e


# --------------------------------------------------------------------------- #
# Merge del corte diario (store) sobre el CSV semilla
# --------------------------------------------------------------------------- #
def test_el_corte_pisa_al_csv(store):
    """El dato fresco manda sobre la semilla, y deja UNA sola entrada por emisor: dos
    entradas con el mismo core empatarían en el matcher y lo dejarían sin ganador."""
    store.record_corte({"Agrofina S.A.": _fila("BB-(arg)", "Negativa")}, date(2026, 9, 1))
    r = rating_for("Agrofina S.A.")
    assert r["rating"] == "BB-(arg)" and r["perspectiva"] == "Negativa"
    assert r["grade"] == "weak"          # el grade se recalcula del rating fresco (CSV: D)
    assert r["as_of"] == "2026-09-01"
    assert len([e for e in ratings._entries() if "Agrofina" in e["emisor"]]) == 1


def test_emisor_que_fix_dejo_de_publicar_sobrevive(store):
    """Agrality y Metalfor ya NO figuran en el listado de FIX (calificación retirada): un
    fallback "store si hay corte, si no CSV" los borraría del panel al primer corte."""
    semilla = {e["emisor"]: e["rating"] for e in ratings._entries()}   # padrón pre-corte
    store.record_corte({"Agrofina S.A.": _fila("D(arg)", "N/A")}, date(2026, 9, 1))
    assert rating_for("Agrality S.A.")["rating"] == semilla["Agrality S.A."]
    metalfor = rating_for("Metalfor S.A.")
    assert metalfor["rating"] == semilla["Metalfor S.A."]
    assert metalfor["as_of"] == ratings.AS_OF    # dato viejo: as_of del CSV, no del corte


def test_emisor_solo_en_el_store_aparece(store):
    """Los ~50 emisores que el CSV no tiene (bancos, financieras cautivas) entran por el
    corte. Sin corte, `test_no_false_positives` fija que Banco Macro no está."""
    store.record_corte(
        {"Banco Macro S.A.": _fila("AA(arg)", sector="Bancos", area="Entidades Financieras")},
        date(2026, 9, 1))
    r = rating_for("BANCO MACRO S.A.")
    assert r["rating"] == "AA(arg)" and r["sector"] == "Bancos"
    assert r["as_of"] == "2026-09-01" and r["source"] == "FIX SCR"


def test_el_merge_toma_el_nombre_de_fix_y_conserva_los_alias_del_csv(store):
    """El emisor canónico pasa a ser la ENTIDAD del corte: es la clave con la que
    `fix_changes` guarda el cambio y por donde el panel joinea el badge. Pero los cores del
    CSV se conservan (unión), así el alias que sólo vive en la semilla sigue matcheando."""
    store.record_corte({"Tango Energy Argentina S.A.": _fila("BBB(arg)")}, date(2026, 9, 1))
    r = rating_for("PETROLERA ACONCAGUA ENERGIA S.A.")   # alias que sólo trae el CSV
    assert r["rating"] == "BBB(arg)"
    assert r["emisor"] == "Tango Energy Argentina S.A."


def test_el_merge_no_colapsa_una_entidad_nueva_parecida(store):
    """La contención (nivel 3 del matcher) es una heurística para la CONSULTA, donde la
    alternativa es quedarse sin rating. En el merge la alternativa es tener dos entradas,
    así que no se usa: colapsar "Tecpetrol Internacional" sobre "Tecpetrol S.A." le pondría
    a la segunda un rating que no es suyo."""
    semilla = rating_for("Tecpetrol S.A.")["rating"]
    store.record_corte({"Tecpetrol Internacional S.L.U.": _fila("BB(arg)", "Negativa")},
                       date(2026, 9, 1))
    assert rating_for("Tecpetrol S.A.")["rating"] == semilla        # la semilla, intacta
    assert rating_for("Tecpetrol Internacional S.L.U.")["rating"] == "BB(arg)"


def test_el_sector_curado_del_csv_le_gana_al_del_corte(store):
    """FIX publica un sector grueso ("Energia"); el del CSV está curado ("Generación
    Eléctrica"). El corte aporta rating y perspectiva, no taxonomía."""
    curado = rating_for("Central Puerto S.A.")["sector"]
    assert curado != "Energia", "el CSV tiene que traer el sector fino para que esto pruebe algo"
    store.record_corte({"Central Puerto S.A.": _fila("AA+(arg)", sector="Energia")},
                       date(2026, 9, 1))
    r = rating_for("Central Puerto S.A.")
    assert r["rating"] == "AA+(arg)"       # el rating sí lo pisa el corte
    assert r["sector"] == curado


def test_la_perspectiva_del_store_pasa_tal_cual(store):
    """El scraper ya normaliza al vocabulario del CSV ("RW Negativo", "N/A"): re-traducir
    acá haría que el primer diff marcara cambios de perspectiva falsos en todo el panel."""
    store.record_corte({"Meranol S.A.C.I.": _fila("A(arg)", "RW Negativo")}, date(2026, 9, 1))
    assert rating_for("Meranol S.A.C.I.")["perspectiva"] == "RW Negativo"


# --------------------------------------------------------------------------- #
# as_of dinámico + invalidación del cache
# --------------------------------------------------------------------------- #
def test_as_of_es_la_fecha_del_corte_y_cae_al_csv_si_no_hay(store):
    assert ratings.as_of() == ratings.AS_OF               # sin corte: el valor del CSV
    store.record_corte({"Agrofina S.A.": _fila("D(arg)", "N/A")}, date(2026, 9, 3))
    assert ratings.as_of() == "2026-09-03"


def test_el_cache_se_invalida_al_cambiar_el_corte(store):
    """El lru_cache vivía por proceso: con el loop diario, el corte de mañana no se vería
    hasta reiniciar el server. La fecha del corte ES parte de la key."""
    store.record_corte({"Agrofina S.A.": _fila("D(arg)", "N/A")}, date(2026, 9, 1))
    assert rating_for("Agrofina S.A.")["rating"] == "D(arg)"
    store.record_corte({"Agrofina S.A.": _fila("CCC(arg)", "Positiva")}, date(2026, 9, 2))
    r = rating_for("Agrofina S.A.")
    assert r["rating"] == "CCC(arg)" and r["as_of"] == "2026-09-02"


def test_invalidate_expone_un_corte_de_la_misma_fecha(store, tmp_path, monkeypatch):
    """Si la fecha NO cambia (re-siembra, otro store, backfill), la key no alcanza:
    `invalidate_cache()` es la palanca explícita que corre el loop después de grabar."""
    from core.infrastructure import ratings_history as rh
    store.record_corte({"Agrofina S.A.": _fila("D(arg)", "N/A")}, date(2026, 9, 1))
    assert rating_for("Agrofina S.A.")["rating"] == "D(arg)"
    otro = rh.RatingsHistoryStore(tmp_path / "otro.db")
    otro.record_corte({"Agrofina S.A.": _fila("B(arg)")}, date(2026, 9, 1))
    monkeypatch.setattr(rh, "_STORE", otro)
    assert rating_for("Agrofina S.A.")["rating"] == "D(arg)"     # misma fecha: cache viejo
    ratings.invalidate_cache()
    assert rating_for("Agrofina S.A.")["rating"] == "B(arg)"


def test_sin_corte_el_panel_sirve_la_semilla_entera(store):
    """El store vacío (primer arranque, o scrape caído desde siempre) no tiene que restar:
    el panel sigue viendo los 75 emisores del CSV con su as_of."""
    import csv
    with open(ratings.CSV_PATH, encoding="utf-8", newline="") as f:
        filas_csv = sum(1 for _ in csv.DictReader(f))
    ents = ratings._entries()
    assert len(ents) == filas_csv          # contra el CSV, no contra un numero magico
    assert ratings.as_of() == ratings.AS_OF
    assert all(e["as_of"] == ratings.AS_OF for e in ents)


def test_rating_for_conserva_cache_clear_para_los_consumidores(store):
    """El cache se mudo de `rating_for`/`_entries` a la funcion interna que lleva el corte
    en la key, pero afuera (el loop de app.py, los helpers de otros modulos de test) se
    invalida llamando `rating_for.cache_clear()`. Ese atributo tiene que seguir existiendo
    y tirar TODO el padron, no solo el matcher."""
    store.record_corte({"Agrofina S.A.": _fila("D(arg)", "N/A")}, date(2026, 9, 1))
    assert rating_for("Agrofina S.A.")["rating"] == "D(arg)"
    assert ratings._entries_cached.cache_info().currsize == 1
    rating_for.cache_clear()
    assert ratings._entries_cached.cache_info().currsize == 0
    ratings._entries()
    ratings._entries.cache_clear()
    assert ratings._entries_cached.cache_info().currsize == 0
