"""Scraper + parser del listado público de FIX SCR (`core/infrastructure/fix_ratings.py`).

Sin red: el parser se ejerce contra la fixture HTML real recortada del sitio
(`tests/fixtures/fixscr_calificaciones.html`, 9 filas, una por variante) y el fetch
contra un `httpx.MockTransport`. Motivo: el scraping es la pieza más frágil de la
feature (el sitio puede cambiar el HTML o bloquear), así que su red de seguridad tiene
que correr en el gate siempre, sin depender de que fixscr.com esté vivo.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from core.infrastructure.fix_ratings import (
    FixParseError,
    FixRow,
    fetch_listado,
    mejor_fila_por_entidad,
    normalizar_perspectiva,
    parse_listado,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fixscr_calificaciones.html"


@pytest.fixture(scope="module")
def filas() -> list[FixRow]:
    return parse_listado(FIXTURE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Helpers de HTML sintético, para las variantes que la fixture no cubre (entidades
# HTML escapadas, paginación). El thead replica los `id="column-*"` de Kartik, que
# es lo que el parser valida.
# --------------------------------------------------------------------------- #

_COLS = [
    ("entidad", "ENTIDAD"), ("fecha", "FECHA"), ("pais", "PAÍS"), ("area", "AREA"),
    ("sector", "SECTOR"), ("tipo-calificacion", "TIPO DE CALIFICACIÓN"),
    ("corto-plazo", "CALIFICACIÓN CORTO PLAZO"), ("largo-plazo", "CALIFICACIÓN LARGO PLAZO"),
    ("perspectiva", "PERSPECTIVA / RATING WATCH"), ("estado", "ESTADO"),
]


def _thead(cols=_COLS) -> str:
    ths = "".join(f'<th id="column-{cid}" data-col-seq="{i}">{lbl}</th>'
                  for i, (cid, lbl) in enumerate(cols))
    return f"<table><thead><tr>{ths}</tr></thead>"


def _fila(entidad, *, fecha="2026-08-06", pais="Argentina", area="Finanzas Corporativas",
          sector="Empresas", tipo="Emisor", cp="", lp="A(arg)",
          persp="Perspectiva Estable", estado="Confirma") -> str:
    celdas = [
        f'<a class="linkName" href="/emisor/view?id=1">{entidad}</a>', fecha, pais, area,
        sector, f'<a class="linkName" href="/emisor/view?id=1">{tipo}</a>', cp, lp,
        f'<div><p style="margin-bottom: 0!important">{persp}<span class="ROSta"></span></p></div>',
        estado,
    ]
    tds = "".join(f'<td class="w4" data-col-seq="{i}">{v}</td>' for i, v in enumerate(celdas))
    return f'<tr class="w4" data-key="">{tds}</tr>'


def _tabla(filas_html: str) -> str:
    return _thead() + "<tbody>" + filas_html + "</tbody></table>"


def _pagina(entidades, **kw) -> str:
    return _tabla("".join(_fila(e, **kw) for e in entidades))


# --------------------------------------------------------------------------- #
# parse_listado
# --------------------------------------------------------------------------- #

def test_parsea_las_nueve_filas_de_la_fixture(filas):
    # 9 <tr data-key>. La fila de filtros del thead también tiene <td>: no debe colarse.
    assert len(filas) == 9


def test_primera_fila_con_todos_los_campos(filas):
    f = filas[0]
    assert f.entidad == "360 Energy Solar S.A. (ex Energias Sustentables S.A.)"
    assert f.fecha == date(2026, 8, 6)
    assert f.pais == "Argentina"
    assert f.area == "Finanzas Corporativas"
    assert f.sector == "Empresas"
    assert f.tipo == "Emisor"
    assert f.rating_cp == ""
    assert f.rating_lp == "A(arg)"
    assert f.perspectiva == "Estable"
    assert f.estado == "Confirma"


def test_entidad_con_acentos_utf8(filas):
    assert any(f.entidad == "AES Argentina Generación S.A." for f in filas)


def test_perspectiva_llega_normalizada_al_vocabulario_del_csv(filas):
    assert {f.perspectiva for f in filas} == {"Estable", "N/A", "Positiva", "Negativa"}


def test_fila_de_endeudamiento_trae_rating_de_corto_plazo(filas):
    meranol = next(f for f in filas if f.entidad.startswith("Meranol"))
    assert meranol.tipo == "Endeudamiento de Largo Plazo"
    assert meranol.rating_cp == "A2(arg)"
    assert meranol.rating_lp == "A(arg)"
    assert meranol.fecha == date(2026, 6, 9)


def test_tipo_se_parsea_aunque_el_anchor_venga_sin_cerrar(filas):
    # El sitio emite `<a ...>Obligaciones ... </td>` (sin </a>): el parser corta por
    # </td>, no por el anchor, así que la celda sigue siendo legible.
    ons = [f for f in filas if f.tipo.startswith("Obligaciones Negociables")]
    assert len(ons) == 2


def test_desescapa_entidades_html():
    html = _pagina(["Medios &amp; Entretenimiento S.A."], sector="Medios &amp; Entretenimiento")
    f = parse_listado(html)[0]
    assert f.entidad == "Medios & Entretenimiento S.A."
    assert f.sector == "Medios & Entretenimiento"


def test_fixrow_es_inmutable(filas):
    with pytest.raises(Exception):
        filas[0].rating_lp = "AAA(arg)"


def test_thead_con_columnas_distintas_levanta_excepcion():
    # Un cambio de estructura tiene que ser RUIDOSO: devolver [] lo haría pasar por
    # "hoy FIX no publicó nada" y el store grabaría un corte vacío.
    cols = [(cid, lbl) for cid, lbl in _COLS if cid != "largo-plazo"]
    html = _thead(cols) + "<tbody>" + _fila("Acme S.A.") + "</tbody></table>"
    with pytest.raises(FixParseError):
        parse_listado(html)


def test_html_sin_tabla_levanta_excepcion():
    with pytest.raises(FixParseError):
        parse_listado("<html><body><h1>Mantenimiento</h1></body></html>")


def test_fila_con_menos_celdas_que_columnas_levanta_excepcion():
    html = _thead() + '<tbody><tr data-key=""><td>Acme S.A.</td><td>2026-08-06</td></tr></tbody></table>'
    with pytest.raises(FixParseError):
        parse_listado(html)


def test_tbody_vacio_devuelve_lista_vacia_sin_levantar():
    # Estructura OK y cero filas es un resultado legítimo (página pasada del final):
    # lo resuelve el corte de paginación, no una excepción.
    assert parse_listado(_thead() + "<tbody></tbody></table>") == []


# --------------------------------------------------------------------------- #
# normalizar_perspectiva
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("crudo,esperado", [
    ("Perspectiva Estable", "Estable"),
    ("Perspectiva Positiva", "Positiva"),
    ("Perspectiva Negativa", "Negativa"),
    ("N.C", "N/A"),
    ("RW Positivo", "RW Positivo"),
    ("RW Negativo", "RW Negativo"),
    ("", "N/A"),
    ("  Perspectiva  Estable ", "Estable"),
])
def test_normalizar_perspectiva(crudo, esperado):
    assert normalizar_perspectiva(crudo) == esperado


# --------------------------------------------------------------------------- #
# mejor_fila_por_entidad
# --------------------------------------------------------------------------- #

def test_mejor_fila_descarta_instrumentos_y_estructurados(filas):
    mejores = mejor_fila_por_entidad(filas)
    assert set(mejores) == {
        "360 Energy Solar S.A. (ex Energias Sustentables S.A.)",
        "Agrofina S.A.",
        "Camuzzi Gas Pampeana S.A.",
        "AES Argentina Generación S.A.",
        "Central Puerto S.A.",
        "Meranol S.A.C.I.",
    }
    # El fideicomiso (Certificados de Participación, A-sf(arg)) no es un emisor.
    assert "FF Proyecto V.I.D.A. - Laboratorios Richmond" not in mejores


def test_mejor_fila_prefiere_emisor_sobre_la_fila_de_instrumento(filas):
    mejores = mejor_fila_por_entidad(filas)
    assert mejores["360 Energy Solar S.A. (ex Energias Sustentables S.A.)"].tipo == "Emisor"
    assert mejores["AES Argentina Generación S.A."].tipo == "Emisor"


def test_mejor_fila_cae_a_endeudamiento_si_no_hay_emisor(filas):
    # 52 de los 125 emisores solo tienen esta fila: filtrar por tipo=Emisor los perdería.
    assert mejor_fila_por_entidad(filas)["Meranol S.A.C.I."].tipo == "Endeudamiento de Largo Plazo"


def test_emisor_gana_aunque_venga_despues_del_endeudamiento():
    filas_ = parse_listado(_tabla(
        _fila("Acme S.A.", tipo="Endeudamiento de Largo Plazo", lp="BBB(arg)")
        + _fila("Acme S.A.", tipo="Emisor", lp="A(arg)")))
    assert mejor_fila_por_entidad(filas_)["Acme S.A."].rating_lp == "A(arg)"


def test_rating_sf_se_excluye_aun_con_tipo_emisor():
    # La marca `sf(arg)` es de finanzas estructuradas: no califica al emisor aunque la
    # fila se declare tipo Emisor.
    filas_ = parse_listado(_pagina(["FF Acme"], tipo="Emisor", lp="AAAsf(arg)"))
    assert mejor_fila_por_entidad(filas_) == {}


def test_filas_sin_rating_de_largo_plazo_se_descartan():
    # El panel muestra el rating de LARGO plazo: una fila Emisor sin LP no aporta nada
    # y, si ganara por tipo, taparía la de Endeudamiento que sí lo trae.
    filas_ = parse_listado(_tabla(
        _fila("Acme S.A.", tipo="Emisor", lp="")
        + _fila("Acme S.A.", tipo="Endeudamiento de Largo Plazo", lp="BBB(arg)")))
    assert mejor_fila_por_entidad(filas_)["Acme S.A."].rating_lp == "BBB(arg)"


def test_desempate_por_fecha_mas_reciente():
    filas_ = parse_listado(_tabla(
        _fila("Acme S.A.", fecha="2026-01-15", lp="BBB(arg)")
        + _fila("Acme S.A.", fecha="2026-08-20", lp="A(arg)")))
    assert mejor_fila_por_entidad(filas_)["Acme S.A."].rating_lp == "A(arg)"


# --------------------------------------------------------------------------- #
# Corte de paginación: filas CRUDAS, no filas parseadas
# --------------------------------------------------------------------------- #

def test_una_fecha_ilegible_no_corta_la_paginacion():
    """Una fila descartada no puede hacer creer que la página venía incompleta.

    `parse_listado` saltea la fila con fecha ilegible (es un problema del dato, no de
    la estructura). Si el corte de paginación mira las filas PARSEADAS, una página
    llena de 50 con UNA fecha mala devuelve 49 → el fetch la toma por última y
    ABANDONA el área en silencio, perdiendo todas las páginas siguientes. El corte
    tiene que mirar las filas CRUDAS que trajo el HTML."""
    llena_con_una_mala = _tabla(
        "".join(_fila(f"Emisor {i:02d}") for i in range(49))
        + _fila("Emisor Con Fecha Mala", fecha="27/02/20**")   # se descarta al parsear
    )
    ultima = _pagina(["Emisor Ultimo"])
    pedidas = []

    def handler(request):
        pagina = int(request.url.params.get("page", 1))
        area = request.url.params.get("CalificacionesWebSearch[section_id]")
        pedidas.append((area, pagina))
        if pagina == 1:
            return httpx.Response(200, text=llena_con_una_mala)
        if pagina == 2:
            return httpx.Response(200, text=ultima)
        return httpx.Response(200, text=_tabla(""))

    filas_ = fetch_listado(client=_mock_client(handler), pausa=0)
    # 49 buenas de la página 1 + 1 de la página 2, por cada una de las 2 áreas.
    assert len(filas_) == 100, f"se perdieron páginas: {len(filas_)}"
    assert (("1", 2) in pedidas), "nunca pidió la página 2: cortó por la fila descartada"


# --------------------------------------------------------------------------- #
# fetch_listado (MockTransport — nunca toca la red)
# --------------------------------------------------------------------------- #

def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_corta_cuando_el_sitio_repite_la_ultima_pagina():
    """El sitio NO da 404 ni vacío pasada la última página: repite la última. Sin este
    corte la paginación no terminaría nunca y duplicaría filas."""
    pedidos: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        area = request.url.params["CalificacionesWebSearch[section_id]"]
        page = request.url.params.get("page", "1")
        pedidos.append((area, page))
        if area == "1":
            # page 1 y 2 distintas; la 3 repite la 2 (comportamiento real del sitio).
            grupo = "E" if page == "1" else "F"
            cuerpo = _pagina([f"{grupo}{i} S.A." for i in range(50)])
        else:
            cuerpo = _pagina([f"B{i} S.A." for i in range(3)])
        return httpx.Response(200, content=cuerpo.encode("utf-8"))

    filas_ = fetch_listado(client=_mock_client(handler), pausa=0)

    assert [p for p in pedidos if p[0] == "1"] == [("1", "1"), ("1", "2"), ("1", "3")]
    assert [p for p in pedidos if p[0] == "2"] == [("2", "1")]   # <50 filas → corta
    assert len(filas_) == 103                                    # la repetida NO se suma
    assert len({f.entidad for f in filas_}) == 103


def test_fetch_manda_los_params_de_argentina_y_las_dos_areas():
    vistos: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistos.append(request.url)
        return httpx.Response(200, content=_pagina(["Acme S.A."]).encode("utf-8"))

    fetch_listado(client=_mock_client(handler), pausa=0)

    assert len(vistos) == 2                                      # 1 página por área
    for url in vistos:
        assert url.path == "/calificaciones"
        assert url.params["CalificacionesWebSearch[paises_id]"] == "230"
        assert url.params["per-page"] == "50"                    # 100+ → HTTP 500 del sitio
    assert {u.params["CalificacionesWebSearch[section_id]"] for u in vistos} == {"1", "2"}


def test_fetch_tiene_tope_duro_de_paginas():
    """Red anti-loop: si el sitio deja de repetir la última página, la paginación
    igual termina."""
    n = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        cuerpo = _pagina([f"E{n}-{i} S.A." for i in range(50)])
        return httpx.Response(200, content=cuerpo.encode("utf-8"))

    filas_ = fetch_listado(client=_mock_client(handler), pausa=0)
    assert n == 2 * 20                                           # tope 20 páginas × 2 áreas
    assert len(filas_) == 40 * 50


def test_fetch_decodifica_utf8_aunque_no_venga_charset():
    """PAÍS viaja como bytes UTF-8 (C3 8D). Sin forzar utf-8, httpx adivina latin-1 y
    los acentos llegan mojibake al matcher de emisores."""
    def handler(request: httpx.Request) -> httpx.Response:
        cuerpo = _pagina(["AES Argentina Generación S.A."]).encode("utf-8")
        return httpx.Response(200, content=cuerpo, headers={"Content-Type": "text/html"})

    filas_ = fetch_listado(client=_mock_client(handler), pausa=0)
    assert filas_ and all(f.entidad == "AES Argentina Generación S.A." for f in filas_)


def test_fetch_propaga_el_error_http():
    # Mejor un día sin corte que medio corte: el loop loguea y reintenta al tick siguiente.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(httpx.HTTPStatusError):
        fetch_listado(client=_mock_client(handler), pausa=0)
