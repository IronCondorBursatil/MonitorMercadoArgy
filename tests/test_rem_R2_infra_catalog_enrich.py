"""Remediación R2 sobre `core/infrastructure/byma/catalog_enrich.py`.

RESIDUO del hallazgo C-#2 — la SEMILLA no puede pisar a la DB
--------------------------------------------------------------
El fix del lote C dejó `want_isin = isin or o.isin`, que tapa el wipe a None pero
sigue dejando que `data/byma/titulos_final.csv` PISE con otro valor un ISIN ya
cargado. Contradice el invariante de CLAUDE.md ("SQLite = fuente de verdad;
Excel/CSV = semillas de bootstrap") y contradice a su propia función hermana,
`enrich_isin_from_ficha`, que hace `if o is None or o.isin: continue`. El ABM es el
editor de RUNTIME: su dato gana. Regla correcta: `o.isin or isin` — la semilla
RELLENA el hueco, nunca corrige.

Hoy hay 0 discrepancias medidas entre CSV y DB, así que el bug es LATENTE: ningún
test de comportamiento sobre los datos actuales lo pone rojo. Por eso hace falta el
caso explícito de discrepancia que hay acá abajo.

Hallazgo #9 (lote F) — `truststore` era código muerto permanente
----------------------------------------------------------------
`enrich_ficha_meta` y `enrich_isin_from_ficha` hacían `import truststore` +
`truststore.inject_into_ssl()` bajo un `except ImportError: pass`. `truststore`
nunca estuvo instalado ni declarado en requirements*, así que el `except`
disimulaba que el workaround NO corría nunca. Se borró (no se declaró la
dependencia) porque el problema que resolvía ya no existe: los hosts BYMA validan
con certifi. En su lugar, la sesión `requests` pasa por la política TLS única del
repo, así que el override `MONITOR_TLS_NO_VERIFY_HOSTS` también manda acá.
"""

from __future__ import annotations

import ast
from pathlib import Path

from core.infrastructure.byma import catalog_enrich as ce
from core.infrastructure.byma.catalog_enrich import enrich_isin_from_byma
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import InstrumentORM

_CSV_HEADER = "symbol;codigoIsin;tipoEspecie;securityType;emisor\n"


def _csv(tmp_path, *rows):
    p = tmp_path / "titulos_final.csv"
    p.write_text(_CSV_HEADER + "".join(rows), encoding="utf-8-sig")
    return p


def _add(**kw):
    init_db()
    with SessionLocal.begin() as s:
        s.add(InstrumentORM(**kw))


def _isin(ticker):
    with SessionLocal() as s:
        return s.get(InstrumentORM, ticker).isin


# ------------------------------------------------ la semilla NO pisa a la DB --

def test_la_semilla_no_pisa_un_isin_distinto_ya_cargado(tmp_db, tmp_path):
    """CSV con OTRO ISIN para un ticker que la DB ya tiene: gana la DB.

    Es el caso que el parche `isin or o.isin` dejaba pasar: con un ISIN presente en
    los dos lados y DISTINTOS, la semilla ganaba. El ABM es el editor de runtime."""
    _add(ticker="AL30", short_name="AL30", instrument_type="BONAR", sheet="Soberanos",
         isin="ARABM00000001")                      # cargado a mano por el ABM
    csv = _csv(tmp_path, "AL30;ARCSV000000X9;Titulos Publicos;GO;Rep. Argentina\n")

    enrich_isin_from_byma(csv)

    assert _isin("AL30") == "ARABM00000001"


def test_la_semilla_igual_no_reescribe_ni_rompe_la_idempotencia(tmp_db, tmp_path):
    """Con el mismo ISIN en los dos lados, la 2ª corrida no reporta cambios."""
    _add(ticker="GD30", short_name="GD30", instrument_type="GLOBAL", sheet="Soberanos",
         isin="ARARGE3209U4")
    csv = _csv(tmp_path, "GD30;ARARGE3209U4;Titulos Publicos;GO;Rep. Argentina\n")

    assert enrich_isin_from_byma(csv) == 1   # 1ª: escribe la metadata byma
    assert enrich_isin_from_byma(csv) == 0   # 2ª: nada que hacer
    assert _isin("GD30") == "ARARGE3209U4"


def test_la_semilla_sigue_rellenando_el_hueco(tmp_db, tmp_path):
    """No-regresión: el propósito de la función (completar el ISIN que falta)."""
    _add(ticker="GD35", short_name="GD35", instrument_type="GLOBAL", sheet="Soberanos")
    csv = _csv(tmp_path, "GD35;ARARGE3209V2;Titulos Publicos;GO;Rep. Argentina\n")

    assert enrich_isin_from_byma(csv) == 1
    assert _isin("GD35") == "ARARGE3209V2"


def test_la_semilla_sin_isin_no_degrada_a_none(tmp_db, tmp_path):
    """No-regresión del fix previo del lote C (el wipe a None)."""
    _add(ticker="ALAAD", short_name="ALAAD", instrument_type="HARD DOLLAR",
         sheet="Obligaciones_Negociables", isin="ARMANUAL00001")
    csv = _csv(tmp_path, "ALAAD;;Obligaciones Negociables;CORP;Aluar\n")

    enrich_isin_from_byma(csv)

    assert _isin("ALAAD") == "ARMANUAL00001"


def test_la_hermana_por_ficha_ya_tenia_esta_regla(tmp_db):
    """`enrich_isin_from_ficha` selecciona SOLO filas con `isin IS NULL`: una fila
    con ISIN ni siquiera es candidata (0 requests). Las dos vías de enriquecimiento
    tienen que coincidir en la regla — DB gana, la fuente rellena — y esta es la
    referencia contra la que se alineó `enrich_isin_from_byma`."""
    from core.infrastructure.byma.catalog_enrich import enrich_isin_from_ficha

    _add(ticker="AE38", short_name="AE38", instrument_type="BONAR", sheet="Soberanos",
         isin="ARDB000000001")
    # sin targets no toca la red (short-circuit antes de crear la Session)
    assert enrich_isin_from_ficha() == 0
    assert _isin("AE38") == "ARDB000000001"


# ------------------------------------------------------ truststore: enterrado --

def test_no_queda_ninguna_referencia_a_truststore():
    """`truststore` no está declarado en ningún requirements: cualquier
    `import truststore` en este módulo es código muerto disimulado por su
    `except ImportError`."""
    src = Path(ce.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imports = [
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in getattr(node, "names", [])
    ]
    assert "truststore" not in imports
    llamadas = [
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert "inject_into_ssl" not in llamadas


def test_truststore_sigue_sin_estar_declarado_en_requirements():
    """Ancla del test de arriba: si algún día SÍ se declara la dependencia, el
    workaround deja de ser código muerto y la decisión hay que revisarla."""
    raiz = Path(ce.__file__).parents[3]
    declarado = []
    for nombre in ("requirements.txt", "requirements.lock", "requirements-dev.txt"):
        f = raiz / nombre
        if not f.is_file():
            continue
        for linea in f.read_text(encoding="utf-8").splitlines():
            pin = linea.split("#", 1)[0].strip()          # descartar comentarios
            if pin.lower().startswith("truststore"):
                declarado.append(f"{nombre}: {linea.strip()}")
    assert declarado == [], (
        "truststore pasó a estar declarado: revisar si conviene reponer "
        "inject_into_ssl() en catalog_enrich (ver el docstring de _ficha_session)."
    )


# ------------------------------------- la sesión de ficha usa la política TLS --

def test_la_sesion_de_ficha_verifica_tls_por_default(monkeypatch):
    monkeypatch.delenv("MONITOR_TLS_NO_VERIFY_HOSTS", raising=False)
    assert ce._ficha_session().verify is True


def test_la_sesion_de_ficha_respeta_el_override_de_tls(monkeypatch):
    """Reemplazo real del workaround muerto: acá SÍ hay una perilla que funciona."""
    monkeypatch.setenv("MONITOR_TLS_NO_VERIFY_HOSTS", "open.bymadata.com.ar")
    assert ce._ficha_session().verify is False


def test_la_sesion_de_ficha_lleva_los_headers_de_byma():
    """No-regresión: el Token/Options de la ficha seguía viviendo en la sesión."""
    s = ce._ficha_session()
    assert s.headers.get("Token") == ce._FICHA_HEADERS["Token"]
    assert s.headers.get("Options") == "technical-details"
