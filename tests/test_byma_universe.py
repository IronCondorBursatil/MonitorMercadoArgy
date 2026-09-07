"""Universo BYMA: ingesta del seed a byma_catalog + buscador (ticker/ISIN/emisor/categoría)."""

import csv
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from apps.web.app import app
from core.infrastructure.byma import universe

_TPL_DIR = Path(__file__).resolve().parent.parent / "apps" / "web" / "templates"


def _render(name, **ctx):
    env = Environment(loader=FileSystemLoader(str(_TPL_DIR)), autoescape=True)
    return env.get_template(name).render(**ctx)


def _sample_rows(n):
    return [{
        "key": f"K{i}", "base": f"T{i:03d}", "isin": f"ISIN{i:03d}",
        "legislacion": "Ley NY" if i % 2 else "Ley Local",
        "categoria": "Obligaciones Negociables", "security_type": "CORP",
        "panel": "Oblig. Negociables", "emisor": "EMISOR S.A.",
        "primary": {"pesos": f"T{i:03d}", "mep": f"T{i:03d}D", "cable": ""},
        "especial": {"pesos": "", "mep": "", "cable": ""},
    } for i in range(n)]

_HEADER = ["symbol", "ticker_pesos", "moneda", "securityType", "sufijo",
           "clase_liquidacion", "segmento", "cotiza", "panel", "codigoIsin",
           "tipoEspecie", "insType", "emisor"]


def _write(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(_HEADER)
        w.writerows(rows)


def test_ingest_and_search(tmp_db):
    csvp = tmp_db / "t.csv"
    _write(csvp, [
        ["GGAL", "GGAL", "ARS", "CS", "L", "primary", "", "True", "Acciones Lideres",
         "ARP495251018", "Acciones", "EQUITY", "GRUPO FINANCIERO GALICIA S.A."],
        ["GGALD", "GGAL", "USD", "CS", "D", "primary", "", "True", "Acciones Lideres",
         "ARP495251018", "Acciones", "EQUITY", "GRUPO FINANCIERO GALICIA S.A."],
        # tipoEspecie vacío → categoría inferida del securityType (GO→Títulos Públicos)
        ["AL30", "AL30", "ARS", "GO", "0", "primary", "", "True", "Titulos Publicos",
         "ARARGE3209S6", "", "BOND", "REP. ARGENTINA"],
        ["ZZZ9", "ZZZ9", "ARS", "CORP", "", "", "", "False", "Oblig. Negociables",
         "", "", "", ""],  # CORP → Obligaciones Negociables
    ])
    assert universe.ingest_byma_catalog(csvp) == 4
    assert universe.count() == 4
    # idempotente sobre filas del CSV (sin last_seen); con estado del job se rechaza sin force
    assert universe.ingest_byma_catalog(csvp) == 4 and universe.count() == 4

    # por ticker (LIKE) → ambas patas
    rows, total = universe.search_byma_catalog("GGAL")
    assert total == 2 and {r["symbol"] for r in rows} == {"GGAL", "GGALD"}
    # por ISIN
    rows, total = universe.search_byma_catalog("ARARGE3209S6")
    assert total == 1 and rows[0]["symbol"] == "AL30"
    # por emisor (case-insensitive)
    _rows, total = universe.search_byma_catalog("galicia")
    assert total == 2
    # categoría inferida
    rows, _ = universe.search_byma_catalog("AL30")
    assert rows[0]["categoria"] == "Títulos Públicos"
    rows, _ = universe.search_byma_catalog("ZZZ9")
    assert rows[0]["categoria"] == "Obligaciones Negociables"
    # filtro por categoría
    _rows, total = universe.search_byma_catalog("", "Títulos Públicos")
    assert total == 1
    # categories() para los chips/select
    cats = dict(universe.categories())
    assert cats.get("Acciones") == 2 and cats.get("Títulos Públicos") == 1


def test_search_pagination(tmp_db):
    rows = [[f"S{i:04d}", f"S{i:04d}", "ARS", "GO", "", "", "", "True", "Letras",
             "", "Letras", "BOND", "ESTADO"] for i in range(10)]
    _write(tmp_db / "t.csv", rows)
    universe.ingest_byma_catalog(tmp_db / "t.csv")
    p0, total = universe.search_byma_catalog("", "", limit=4, offset=0)
    p1, _ = universe.search_byma_catalog("", "", limit=4, offset=4)
    assert total == 10 and len(p0) == 4 and len(p1) == 4
    assert {r["symbol"] for r in p0}.isdisjoint({r["symbol"] for r in p1})


def test_universe_endpoints_smoke():
    with TestClient(app) as c:
        r = c.get("/abm")
        assert r.status_code == 200 and "Universo BYMA" in r.text
        assert c.get("/abm/universe").status_code == 200
        assert c.get("/abm/universe?q=AL30&cat=&page=0").status_code == 200
        # append (page>0) renderiza solo filas, sin re-armar la tabla
        assert c.get("/abm/universe?page=1").status_code == 200


# ---- scroll infinito (render determinístico de los fragments) ------------- #

def test_rows_partial_sentinel_when_more():
    """Con has_next el partial agrega el centinela que carga el lote siguiente,
    con q/cat/page propagados en su propia URL (autocontenido)."""
    html = _render("fragments/abm_universe_rows.html",
                   rows=_sample_rows(3), q="al30", cat="Obligaciones Negociables",
                   page=0, has_next=True)
    assert html.count('class="uni-tk"') == 3          # 3 filas de datos
    assert 'class="uni-more"' in html                 # centinela presente
    assert 'intersect once' in html and 'hx-swap="outerHTML"' in html
    assert 'page=1' in html                           # carga la página siguiente
    assert 'q=al30' in html and 'cat=Obligaciones' in html  # filtros propagados


def test_rows_partial_no_sentinel_on_last_batch():
    """Último lote (has_next False) → sin centinela: el scroll infinito frena."""
    html = _render("fragments/abm_universe_rows.html",
                   rows=_sample_rows(2), q="", cat="", page=6, has_next=False)
    assert html.count('class="uni-tk"') == 2
    assert 'uni-more' not in html


def test_shell_has_thead_and_first_batch_no_pager():
    """El shell trae el header de 2 niveles + el 1er lote embebido (vía include)
    y ya NO trae paginador."""
    html = _render("fragments/abm_universe.html",
                   rows=_sample_rows(2), q="", cat="", page=0, has_next=True, total=2)
    assert "<thead" in html and "Primary" in html and "Especiales" in html
    assert html.count('class="uni-tk"') == 2          # filas embebidas
    assert 'class="uni-more"' in html                 # centinela del 1er lote
    assert "2 títulos" in html                         # meta sin rango
    assert "uni-pager" not in html and "Siguiente" not in html  # sin paginador


def test_shell_empty_results():
    html = _render("fragments/abm_universe.html",
                   rows=[], q="zzz", cat="", page=0, has_next=False, total=0)
    assert "Sin resultados" in html and "uni-more" not in html


# ---- columna Legislación (solo Obligaciones Negociables) ------------------ #

def test_grouped_marks_has_ficha(tmp_db):
    """Los grupos cuyo ISIN ya tiene ficha técnica rica se marcan has_ficha=True
    (→ ticker naranja); los demás False."""
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM
    _write(tmp_db / "t.csv", [
        ["AEC2O", "AEC2O", "ARS", "CORP", "O", "primary", "", "True",
         "Oblig. Negociables", "USP1000CAE41", "Obligaciones Negociables", "BOND", "AES"],
        ["XYZ0", "XYZ0", "ARS", "CORP", "O", "primary", "", "True",
         "Oblig. Negociables", "AR9999999999", "Obligaciones Negociables", "BOND", "OTRO"],
    ])
    universe.ingest_byma_catalog(tmp_db / "t.csv")
    init_db()
    with SessionLocal.begin() as s:
        s.merge(InstrumentORM(ticker="AEC2O", isin="USP1000CAE41",
                              raw_fields={"byma": {"ficha": {"ley": "Extranjera"}}}))
        s.merge(InstrumentORM(ticker="XYZ0", isin="AR9999999999",
                              raw_fields={"byma": {}}))   # sin ficha
    rows, _ = universe.search_byma_grouped("", "Obligaciones Negociables")
    by = {g["base"]: g for g in rows}
    assert by["AEC2O"]["has_ficha"] is True
    assert by["XYZ0"]["has_ficha"] is False


def test_rows_orange_ticker_only_when_has_ficha():
    rows = _sample_rows(2)
    rows[0]["has_ficha"] = True                            # solo la 1ª tiene ficha
    html = _render("fragments/abm_universe_rows.html", rows=rows, q="",
                   cat="Obligaciones Negociables", page=0, has_next=False)
    assert html.count("uni-tk--ficha") == 1               # una sola fila en naranja


def test_legislacion_derivation():
    assert universe._legislacion("USP1000CAE41") == "Ley NY"
    assert universe._legislacion("AR0561623023") == "Ley Local"
    assert universe._legislacion("XS1234567890") == ""   # internacional → —
    assert universe._legislacion(None) == ""


def test_legislacion_column_shown_for_on():
    """Con show_leg el shell agrega la cabecera Legislación (entre Ticker e ISIN)
    y el partial la celda; el centinela ajusta colspan a 13."""
    shell = _render("fragments/abm_universe.html",
                    rows=_sample_rows(2), q="", cat="Obligaciones Negociables",
                    page=0, has_next=True, total=2, show_leg=True)
    assert "Legislación" in shell
    # Ticker viene antes que Legislación, y Legislación antes que ISIN
    assert shell.index("Ticker") < shell.index("Legislación") < shell.index("ISIN")
    rows = _render("fragments/abm_universe_rows.html",
                   rows=_sample_rows(2), q="", cat="Obligaciones Negociables",
                   page=0, has_next=True, show_leg=True)
    assert 'class="uni-leg"' in rows and "Ley Local" in rows and "Ley NY" in rows
    assert 'colspan="17"' in rows   # 16 + Legislación (Alta? + 3 cols monto hoy ARS/MEP/CABLE)


def test_legislacion_column_hidden_for_other_categories():
    """Sin show_leg (otra categoría / Todas) la columna no aparece; colspan 12."""
    shell = _render("fragments/abm_universe.html",
                    rows=_sample_rows(2), q="", cat="", page=0,
                    has_next=True, total=2, show_leg=False)
    assert "Legislación" not in shell
    rows = _render("fragments/abm_universe_rows.html",
                   rows=_sample_rows(2), q="", cat="", page=0,
                   has_next=True, show_leg=False)
    assert 'class="uni-leg"' not in rows and 'colspan="16"' in rows


def test_search_grouped(tmp_db):
    """1 fila por título valor: agrupa por ISIN y reparte cada moneda en su columna,
    separando la clase `primary` de las `especial`/`otro`."""
    _write(tmp_db / "t.csv", [
        # AEC2: 1 ISIN, 6 especies (3 primary + 3 especial)
        ["AEC2O", "AEC2O", "ARS",   "CORP", "O", "primary",  "", "True",
         "Oblig. Negociables", "USP1000CAE41", "Obligaciones Negociables", "BOND", "AES ARGENTINA"],
        ["AEC2D", "AEC2O", "MEP",   "CORP", "D", "primary",  "", "True",
         "Oblig. Negociables", "USP1000CAE41", "Obligaciones Negociables", "BOND", "AES ARGENTINA"],
        ["AEC2C", "AEC2O", "cable", "CORP", "C", "primary",  "", "True",
         "Oblig. Negociables", "USP1000CAE41", "Obligaciones Negociables", "BOND", "AES ARGENTINA"],
        ["AEC2X", "AEC2O", "ARS",   "CORP", "X", "especial", "", "True",
         "Oblig. Negociables", "USP1000CAE41", "Obligaciones Negociables", "BOND", "AES ARGENTINA"],
        ["AEC2Y", "AEC2O", "MEP",   "CORP", "Y", "especial", "", "True",
         "Oblig. Negociables", "USP1000CAE41", "Obligaciones Negociables", "BOND", "AES ARGENTINA"],
        ["AEC2Z", "AEC2O", "cable", "CORP", "Z", "especial", "", "True",
         "Oblig. Negociables", "USP1000CAE41", "Obligaciones Negociables", "BOND", "AES ARGENTINA"],
        # GGAL: equity, la pata `otro` (GGALB) cae también en especiales
        ["GGAL",  "GGAL", "ARS",   "CS", "",  "primary", "", "True",
         "Acciones", "ARP495251018", "Acciones", "EQUITY", "GRUPO GALICIA"],
        ["GGALD", "GGAL", "MEP",   "CS", "D", "primary", "", "True",
         "Acciones", "ARP495251018", "Acciones", "EQUITY", "GRUPO GALICIA"],
        ["GGALC", "GGAL", "cable", "CS", "C", "primary", "", "True",
         "Acciones", "ARP495251018", "Acciones", "EQUITY", "GRUPO GALICIA"],
        ["GGALB", "GGAL", "ARS",   "CS", "B", "otro",    "", "True",
         "Acciones", "ARP495251018", "Acciones", "EQUITY", "GRUPO GALICIA"],
    ])
    universe.ingest_byma_catalog(tmp_db / "t.csv")

    rows, total = universe.search_byma_grouped()
    assert total == 2  # 2 títulos: AEC2 (ON) + GGAL (acción)
    by_base = {g["base"]: g for g in rows}

    aec = by_base["AEC2O"]
    assert aec["isin"] == "USP1000CAE41"
    assert aec["primary"]  == {"pesos": "AEC2O", "mep": "AEC2D", "cable": "AEC2C"}
    assert aec["especial"] == {"pesos": "AEC2X", "mep": "AEC2Y", "cable": "AEC2Z"}

    ggal = by_base["GGAL"]
    assert ggal["primary"] == {"pesos": "GGAL", "mep": "GGALD", "cable": "GGALC"}
    # clase `otro` (GGALB) cae en especiales/pesos; el resto vacío
    assert ggal["especial"] == {"pesos": "GGALB", "mep": "", "cable": ""}

    # legislación inferida del prefijo de país del ISIN (AR→local, US→NY)
    assert aec["legislacion"] == "Ley NY"      # USP1000CAE41
    assert ggal["legislacion"] == "Ley Local"  # ARP495251018

    # buscar por una pata especial trae el GRUPO completo (todas sus columnas)
    rows, total = universe.search_byma_grouped("AEC2Y")
    assert total == 1 and rows[0]["primary"]["pesos"] == "AEC2O"
    # por emisor (case-insensitive)
    _r, total = universe.search_byma_grouped("galicia")
    assert total == 1
    # filtro por categoría (consistente dentro del grupo)
    _r, total = universe.search_byma_grouped("", "Acciones")
    assert total == 1

    # paginado por GRUPO (no por especie)
    p0, total = universe.search_byma_grouped("", "", limit=1, offset=0)
    p1, _ = universe.search_byma_grouped("", "", limit=1, offset=1)
    assert total == 2 and len(p0) == 1 and len(p1) == 1
    assert p0[0]["base"] != p1[0]["base"]


def test_count_unloaded_matches_grouped_loaded(tmp_db):
    """C1: el badge 'N sin cargar' cuenta TÍTULOS VALOR (grupos) sin cargar, igual que
    los g['loaded'] de la vista agrupada — NO símbolos (que sobrecontaba las patas)."""
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import InstrumentORM
    _write(tmp_db / "t.csv", [
        # AEC2: ON 3 patas, mismo ISIN → 1 grupo, CARGADO por ISIN
        ["AEC2O", "AEC2O", "ARS",   "CORP", "O", "primary", "", "True",
         "Oblig. Negociables", "USP1000CAE41", "Obligaciones Negociables", "BOND", "AES"],
        ["AEC2D", "AEC2O", "MEP",   "CORP", "D", "primary", "", "True",
         "Oblig. Negociables", "USP1000CAE41", "Obligaciones Negociables", "BOND", "AES"],
        ["AEC2C", "AEC2O", "cable", "CORP", "C", "primary", "", "True",
         "Oblig. Negociables", "USP1000CAE41", "Obligaciones Negociables", "BOND", "AES"],
        # GGAL: acción 2 patas → 1 grupo, SIN cargar
        ["GGAL",  "GGAL", "ARS", "CS", "",  "primary", "", "True",
         "Acciones", "ARP495251018", "Acciones", "EQUITY", "GALICIA"],
        ["GGALD", "GGAL", "MEP", "CS", "D", "primary", "", "True",
         "Acciones", "ARP495251018", "Acciones", "EQUITY", "GALICIA"],
        # XYZ0: ON 1 pata → 1 grupo, SIN cargar
        ["XYZ0", "XYZ0", "ARS", "CORP", "O", "primary", "", "True",
         "Oblig. Negociables", "AR9999999999", "Obligaciones Negociables", "BOND", "OTRO"],
        # NOIS: ON 2 patas SIN ISIN → 1 grupo, CARGADO solo por la pata MEP (NOISD)
        ["NOIS",  "NOIS", "ARS", "CORP", "O", "primary", "", "True",
         "Oblig. Negociables", "", "Obligaciones Negociables", "BOND", "BANCO"],
        ["NOISD", "NOIS", "MEP", "CORP", "D", "primary", "", "True",
         "Oblig. Negociables", "", "Obligaciones Negociables", "BOND", "BANCO"],
    ])
    universe.ingest_byma_catalog(tmp_db / "t.csv")
    init_db()
    with SessionLocal.begin() as s:
        s.merge(InstrumentORM(ticker="AEC2O", isin="USP1000CAE41"))     # AEC2 cargado por ISIN
        s.merge(InstrumentORM(ticker="NOISBOND", ticker_mep="NOISD"))   # NOIS cargado por pata MEP

    grouped, _ = universe.search_byma_grouped()
    expected = sum(1 for g in grouped if not g["loaded"])
    assert expected == 2                        # GGAL + XYZ0 (AEC2 y NOIS están cargados)
    assert universe.count_unloaded() == 2       # por GRUPO, no por símbolo (sería 4)
    assert universe.count_unloaded() == expected


# ---- refresh manual del monto operado (botón, no automático) -------------- #

def test_abm_page_has_manual_refresh_button():
    """El universo tiene un botón de refresh manual que re-pide /abm/universe con
    la búsqueda y categoría actuales; el contenedor de resultados NO se
    auto-refresca (sin polling `every` ni `sse` en su trigger)."""
    import re
    with TestClient(app) as c:
        html = c.get("/abm").text
    m = re.search(r'<button[^>]*id="uni-refresh"[^>]*>', html)
    assert m, "falta el botón #uni-refresh en la vista Universo"
    tag = m.group(0)
    assert 'hx-get="/abm/universe"' in tag
    assert 'hx-target="#uni-results"' in tag
    assert "#uni-q" in tag and "#uni-cat" in tag      # propaga q + cat
    m2 = re.search(r'<div id="uni-results"[^>]*>', html)
    assert m2 and "every" not in m2.group(0) and "sse" not in m2.group(0)


def test_universe_shell_shows_asof_time():
    """El shell muestra la hora del snapshot (act HH:MM:SS) en la meta, para saber
    de cuándo es el monto operado que se está mirando."""
    html = _render("fragments/abm_universe.html", rows=_sample_rows(1), q="", cat="",
                   page=0, has_next=False, total=1, asof="14:03:22")
    assert "act 14:03:22" in html


def test_reingerir_con_estado_del_job_se_rechaza_salvo_force(tmp_db):
    """Desde el job diario `byma_catalog` tiene estado propio (`last_seen`, símbolos del
    feed, ficha): re-sembrar del CSV lo pisaría. Sólo con `force=True`."""
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import BymaCatalogORM
    csvp = tmp_db / "t.csv"
    _write(csvp, [["AL30", "AL30", "ARS", "GO", "0", "primary", "", "True", "Titulos Publicos",
                   "ARARGE3209S6", "", "BOND", "REP. ARGENTINA"]])
    assert universe.ingest_byma_catalog(csvp) == 1
    init_db()
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="S29E7", categoria="Títulos Públicos", last_seen="2026-09-07"))
    with pytest.raises(RuntimeError, match="last_seen"):
        universe.ingest_byma_catalog(csvp)
    assert universe.count() == 2                       # no tocó nada
    assert universe.ingest_byma_catalog(csvp, force=True) == 1
    assert universe.count() == 1


# ── prefill enriquecido (novedades, spec 2026-09-07 §3) ─────────────────────
def _uni_row(symbol, **kw):
    from core.infrastructure.db.models import BymaCatalogORM
    base = dict(symbol=symbol, ticker_pesos=symbol, moneda="ARS", clase_liquidacion="primary",
                cotiza=1, categoria="Títulos Públicos", panel="Letras")
    base.update(kw)
    return BymaCatalogORM(**base)


def test_una_letra_del_panel_letras_prefillea_tasa_fija_con_clase_y_vencimiento(tmp_db):
    from core.domain.instrument_groups import is_known_type
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    init_db()
    with SessionLocal.begin() as s:
        s.add(_uni_row("S29E7", vencimiento="2027-01-29"))
        s.add(_uni_row("T15E7", vencimiento="2027-01-15"))
    pf = universe.prefill_for("S29E7")
    assert pf["sheet"] == "Tasa_Fija"
    assert pf["fields"]["clase"] == "LECAP" and is_known_type("LECAP")
    assert pf["fields"]["fecha_pago"] == "2027-01-29"
    assert pf["fields"]["ticker_ars"] == "S29E7"
    assert universe.prefill_for("T15E7")["fields"]["clase"] == "BONCAP"


def test_un_bonte_o_dual_del_panel_letras_no_recibe_una_clase_inventada(tmp_db):
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    init_db()
    with SessionLocal.begin() as s:
        s.add(_uni_row("TO26"))
        s.add(_uni_row("TTM26"))
    for sym in ("TO26", "TTM26"):
        pf = universe.prefill_for(sym)
        assert pf["sheet"] == "Soberanos" and "clase" not in pf["fields"], sym
    assert universe._clase_letra("TY30P") is None
    assert universe._clase_letra("S31G6") == "LECAP" and universe._clase_letra("T30J7") == "BONCAP"


def test_el_vencimiento_de_la_ficha_prefillea_fecha_vencimiento_en_una_on(tmp_db):
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    init_db()
    with SessionLocal.begin() as s:
        s.add(_uni_row("YMCXO", categoria="Obligaciones Negociables", panel="Oblig. Negociables",
                       isin="ARYPFS000001", emisor="YPF S.A.", vencimiento="2029-06-30"))
    pf = universe.prefill_for("YMCXO")
    assert pf["sheet"] == "Obligaciones_Negociables"
    assert pf["fields"]["fecha_vencimiento"] == "2029-06-30"
    assert pf["fields"]["tipo"] == "HARD DOLLAR" and pf["fields"]["short_name"] == "YPF S.A."
