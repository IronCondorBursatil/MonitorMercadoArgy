"""Listado público de calificaciones de FIX SCR (scraper + parser).

Fuente del corte diario de calificaciones que reemplaza la edición manual de
`data/calificaciones.csv`. El sitio es **Yii2 + Kartik GridView, SSR**: no hay API
JSON, así que se lee el HTML de la grilla (verificado en vivo 2026-08-31):

    https://www.fixscr.com/calificaciones
      ?CalificacionesWebSearch[paises_id]=230        (Argentina)
      &CalificacionesWebSearch[section_id]=1|2       (Finanzas Corporativas | Entidades
                                                      Financieras — las 2 áreas del panel ON)
      &per-page=50&page=N

Tres decisiones que vienen de haber medido el sitio, no de suponer:

* **`per-page` topea en 50** — con 100 o más responde HTTP 500. 638 filas salen en 14
  requests: barato para 1×/día.
* **La paginación no termina con 404 ni con una página vacía**: pasada la última, el
  sitio **repite la última**. Por eso el corte es "se repitió la primera entidad" (más
  el tope duro de páginas como red anti-loop).
* **El HTML viene en UTF-8 real** (PAÍS viaja como C3 8D). Se decodifica explícito: si
  se deja adivinar, los acentos llegan mojibake y el matcher de emisores no engancha.

El parseo es con expresiones regulares a propósito: el proyecto no tiene BeautifulSoup
ni lxml en `requirements.lock` (y el HTML de FIX trae anchors sin cerrar, `<a …>texto
</td>`, que un parser estricto rechazaría). Cortando por `</td>` eso no molesta.

División de responsabilidades: `parse_listado` es **pura** (testeada contra una fixture
real del sitio) y `fetch_listado` solo pagina. Es sync: se llama desde el loop diario
vía `to_thread`, como el resto de los providers sync.
"""

from __future__ import annotations

import html as _html
import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Sequence

import httpx

from core.infrastructure._tls import should_verify

logger = logging.getLogger(__name__)

URL = "https://www.fixscr.com/calificaciones"
PAIS_ARGENTINA = 230
AREAS = (1, 2)          # 1 = Finanzas Corporativas, 2 = Entidades Financieras
PER_PAGE = 50           # tope real del sitio: 100+ devuelve HTTP 500
MAX_PAGINAS = 20        # red anti-loop (el máximo observado es 9 páginas por área)
PAUSA_SEG = 1.0         # cortesía entre requests: 1×/día no justifica apurar al sitio
TIMEOUT_SEG = 30.0

# User-Agent de browser: el sitio es una web pública pensada para navegadores y un UA
# de librería es la excusa más barata para que un WAF corte el scrape.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "es-AR,es;q=0.9",
}


class FixParseError(RuntimeError):
    """El HTML de FIX no tiene la forma esperada.

    Se levanta en vez de devolver `[]` a propósito: una lista vacía se confundiría con
    "hoy FIX no publicó nada" y el store grabaría un corte vacío que borraría todos los
    ratings del panel. Un cambio de estructura tiene que ser ruidoso."""


@dataclass(frozen=True, slots=True)
class FixRow:
    """Una fila de la grilla. `perspectiva` YA viene normalizada al vocabulario del CSV
    (ver `normalizar_perspectiva`): se guarda un solo vocabulario para no tener que
    normalizar de nuevo en cada consumidor."""
    entidad: str
    fecha: date
    pais: str
    area: str
    sector: str
    tipo: str
    rating_cp: str
    rating_lp: str
    perspectiva: str
    estado: str


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

# Columnas por su `id="column-*"` de Kartik (no por el rótulo visible): el id es el
# nombre lógico de la columna y sobrevive a un cambio cosmético del encabezado, que es
# justo lo que NO queremos que rompa el scrape.
_COLUMNAS = ("entidad", "fecha", "pais", "area", "sector", "tipo-calificacion",
             "corto-plazo", "largo-plazo", "perspectiva", "estado")

_THEAD_RE = re.compile(r"<thead\b[^>]*>(.*?)</thead>", re.S | re.I)
_TH_RE = re.compile(r"<th\b[^>]*>", re.I)
_COL_ID_RE = re.compile(r'\bid="column-([a-z0-9-]+)"', re.I)
_COL_SEQ_RE = re.compile(r'\bdata-col-seq="(\d+)"', re.I)
# Solo las filas de datos: la grilla mete además una fila de filtros (con <td>) dentro
# del <thead>, y esa no tiene data-key.
_FILA_RE = re.compile(r"<tr\b[^>]*\bdata-key=[^>]*>(.*?)</tr>", re.S | re.I)
_CELDA_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]*>")


def _texto(fragmento: str) -> str:
    """Texto plano de una celda: se sacan los tags, se desescapan las entidades HTML
    (`&amp;` → `&`) y se colapsan los espacios. El unescape va DESPUÉS de sacar tags
    para que un `&lt;b&gt;` del contenido no se convierta en markup."""
    return " ".join(_html.unescape(_TAG_RE.sub(" ", fragmento)).split())


def _indice_columnas(html_doc: str) -> Dict[str, int]:
    """`{nombre lógico de columna: índice de <td>}` leído del `<thead>`.

    Se mapea por posición declarada (`data-col-seq`, con el orden del `<th>` como
    respaldo) en lugar de asumir el orden fijo: así, si FIX agrega o mueve una columna,
    las que nos importan se siguen leyendo bien, y si BORRA alguna, revienta."""
    m = _THEAD_RE.search(html_doc)
    if not m:
        raise FixParseError("El HTML de FIX no trae <thead>: ¿cambió la página o vino un error/captcha?")
    indice: Dict[str, int] = {}
    for orden, th in enumerate(_TH_RE.finditer(m.group(1))):
        col = _COL_ID_RE.search(th.group(0))
        if not col:
            continue
        seq = _COL_SEQ_RE.search(th.group(0))
        indice[col.group(1).lower()] = int(seq.group(1)) if seq else orden
    faltan = [c for c in _COLUMNAS if c not in indice]
    if faltan:
        raise FixParseError(
            f"El listado de FIX ya no trae las columnas {faltan} (vistas: {sorted(indice)}). "
            "Revisar el HTML del sitio antes de confiar en el corte.")
    return indice


def contar_filas_crudas(html_doc: str) -> int:
    """Filas `<tr data-key>` que trae el HTML, ANTES de descartar ninguna.

    Existe para el corte de paginación: `parse_listado` puede devolver menos filas de
    las que vinieron (saltea las de fecha ilegible), y comparar ESE número contra
    `PER_PAGE` hacía que una sola fecha mala en una página llena la hiciera pasar por
    la última del área — el fetch abandonaba en silencio todas las páginas siguientes."""
    return sum(1 for _ in _FILA_RE.finditer(html_doc))


def parse_listado(html_doc: str) -> List[FixRow]:
    """Filas de la grilla de calificaciones. **Función pura** (sin red, sin estado).

    Levanta `FixParseError` si la estructura cambió; devolver `[]` queda reservado para
    el caso legítimo de una página sin filas (la que sigue a la última)."""
    cols = _indice_columnas(html_doc)
    ultima_col = max(cols[c] for c in _COLUMNAS)

    filas: List[FixRow] = []
    crudas = 0
    for m in _FILA_RE.finditer(html_doc):
        crudas += 1
        celdas = [_texto(c) for c in _CELDA_RE.findall(m.group(1))]
        if len(celdas) <= ultima_col:
            raise FixParseError(
                f"Fila con {len(celdas)} celdas y se esperaban al menos {ultima_col + 1}: "
                "cambió la estructura de la grilla.")
        crudo_fecha = celdas[cols["fecha"]]
        try:
            fecha = date.fromisoformat(crudo_fecha)
        except ValueError:
            # Una fecha ilegible es un problema del dato, no de la estructura: se saltea
            # la fila para no perder el corte entero (el guard de abajo cubre el caso de
            # que se vuelvan ilegibles TODAS).
            logger.warning("FIX SCR: fecha ilegible %r en %r — fila descartada",
                           crudo_fecha, celdas[cols["entidad"]])
            continue
        filas.append(FixRow(
            entidad=celdas[cols["entidad"]],
            fecha=fecha,
            pais=celdas[cols["pais"]],
            area=celdas[cols["area"]],
            sector=celdas[cols["sector"]],
            tipo=celdas[cols["tipo-calificacion"]],
            rating_cp=celdas[cols["corto-plazo"]],
            rating_lp=celdas[cols["largo-plazo"]],
            perspectiva=normalizar_perspectiva(celdas[cols["perspectiva"]]),
            estado=celdas[cols["estado"]],
        ))
    if crudas and not filas:
        raise FixParseError(
            f"Se vieron {crudas} filas y ninguna se pudo parsear (¿cambió el formato de fecha?).")
    return filas


# --------------------------------------------------------------------------- #
# Normalización
# --------------------------------------------------------------------------- #

_PREFIJO_PERSPECTIVA = re.compile(r"^perspectiva\s+", re.I)
_SIN_PERSPECTIVA = {"N.C", "N.C.", "NC", "N/A", "-", "--"}


def normalizar_perspectiva(txt: Optional[str]) -> str:
    """Vocabulario de FIX → el de `data/calificaciones.csv`.

    FIX escribe "Perspectiva Estable" y "N.C"; el CSV (y el panel) usan "Estable" y
    "N/A". "RW Positivo"/"RW Negativo" ya coinciden y pasan tal cual. Sin esto, el
    corte del scraper y la semilla del CSV convivirían con dos vocabularios y todo
    emisor que pasara de uno a otro se vería como un cambio de perspectiva falso."""
    limpio = " ".join((txt or "").split())
    if not limpio or limpio.upper() in _SIN_PERSPECTIVA:
        return "N/A"
    return _PREFIJO_PERSPECTIVA.sub("", limpio) or "N/A"


# --------------------------------------------------------------------------- #
# Política de fila por entidad
# --------------------------------------------------------------------------- #

# Tipos que califican al EMISOR, en orden de preferencia. De las 142 entidades del
# listado solo 73 tienen fila "Emisor": quedarse solo con esas perdería 52 emisores que
# únicamente traen "Endeudamiento de Largo Plazo". Es allowlist y no denylist a
# propósito — un tipo nuevo de instrumento no se debe colar por omisión.
_TIPOS_EMISOR = {"emisor": 0, "endeudamiento de largo plazo": 1}

# Finanzas estructuradas: el sufijo `sf` (structured finance) marca la calificación de
# un FIDEICOMISO / emisión, no la del emisor. Se excluye siempre, aun si la fila viene
# tipada como Emisor.
_SF_RE = re.compile(r"sf\s*\(arg\)\s*$", re.I)


def mejor_fila_por_entidad(rows: Sequence[FixRow]) -> Dict[str, FixRow]:
    """`{entidad: mejor fila}` — la calificación que representa al emisor.

    Gana la fila "Emisor"; si no hay, "Endeudamiento de Largo Plazo". Se descartan las
    filas de instrumento (ON Clase X, Certificados de Participación, Acciones…), las de
    finanzas estructuradas (`…sf(arg)`) y las que no traen rating de largo plazo, que
    es el que muestra el panel. A igual tipo gana la fecha más reciente."""
    mejores: Dict[str, tuple] = {}
    for r in rows:
        rango = _TIPOS_EMISOR.get(r.tipo.strip().lower())
        if rango is None:
            continue
        if not r.rating_lp:
            continue
        if _SF_RE.search(r.rating_lp) or _SF_RE.search(r.rating_cp):
            continue
        clave = (rango, -r.fecha.toordinal())
        previa = mejores.get(r.entidad)
        if previa is None or clave < previa[0]:
            mejores[r.entidad] = (clave, r)
    return {entidad: par[1] for entidad, par in mejores.items()}


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #

def _fetch_area(cli: httpx.Client, area: int, pausa: float) -> List[FixRow]:
    """Todas las páginas de un área. Ver el corte de paginación en el docstring del módulo."""
    filas: List[FixRow] = []
    primera_previa: Optional[str] = None
    for pagina in range(1, MAX_PAGINAS + 1):
        if pagina > 1 and pausa:
            time.sleep(pausa)
        resp = cli.get(URL, params={
            "CalificacionesWebSearch[paises_id]": PAIS_ARGENTINA,
            "CalificacionesWebSearch[section_id]": area,
            "per-page": PER_PAGE,
            "page": pagina,
        })
        resp.raise_for_status()
        # UTF-8 explícito: el sitio no siempre declara charset y httpx adivinaría.
        cuerpo = resp.content.decode("utf-8", errors="replace")
        crudas = contar_filas_crudas(cuerpo)
        if not crudas:
            break
        pag = parse_listado(cuerpo)
        if pag and pag[0].entidad == primera_previa:
            # El sitio repite la última página en vez de cortar: ya la teníamos.
            break
        filas.extend(pag)
        if pag:
            primera_previa = pag[0].entidad
        # CRUDAS, no `len(pag)`: una fila descartada por fecha ilegible no significa
        # que la página viniera incompleta (ver contar_filas_crudas).
        if crudas < PER_PAGE:
            break
    else:
        logger.warning("FIX SCR: área %s alcanzó el tope de %s páginas", area, MAX_PAGINAS)
    return filas


def fetch_listado(client: Optional[httpx.Client] = None, *, pausa: float = PAUSA_SEG) -> List[FixRow]:
    """Listado completo de Argentina para las dos áreas del panel ON (crudo, sin filtrar
    por entidad: eso lo hace `mejor_fila_por_entidad`).

    `client` se inyecta en los tests (`httpx.MockTransport`); en producción se arma uno
    propio con la política TLS del repo (`_tls.should_verify`, verify por defecto).
    Un error HTTP se propaga: mejor un día sin corte que medio corte —el guard de
    sanidad del store descartaría igual un corte truncado."""
    propio = client is None
    cli = client or httpx.Client(verify=should_verify(URL), timeout=TIMEOUT_SEG,
                                 headers=_HEADERS, follow_redirects=True)
    try:
        filas: List[FixRow] = []
        for area in AREAS:
            filas.extend(_fetch_area(cli, area, pausa))
        logger.info("FIX SCR: %s filas del listado (%s áreas)", len(filas), len(AREAS))
        return filas
    finally:
        if propio:
            cli.close()
