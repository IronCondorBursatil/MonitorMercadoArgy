"""Auditoría lote C — `enrich_isin_from_byma` degrada datos en cada arranque.

Hallazgo #1: ASIGNA (no fusiona) `raw_fields['byma']`, borrando la sub-clave
`ficha` que puebla `enrich_ficha_meta`. Como el short-circuit de idempotencia
compara el sub-dict completo, la rama de escritura corre SIEMPRE para esas filas:
el docstring "idempotente" es falso y cada boot re-scrapea ~177 fichas.

Hallazgo #2: `o.isin = isin` incondicional, con `isin = meta.get("isin") or None`
→ para un símbolo del CSV semilla SIN `codigoIsin` escribe None ENCIMA del ISIN
cargado a mano por el ABM. Contradice "SQLite = fuente de verdad, CSV = semilla".
"""

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


def _get(ticker):
    with SessionLocal() as s:
        o = s.get(InstrumentORM, ticker)
        return o.isin, dict(o.raw_fields or {})


def test_enrich_no_borra_la_ficha_ya_guardada(tmp_db, tmp_path):
    """La sub-clave `ficha` (de enrich_ficha_meta) debe sobrevivir al enrich del CSV."""
    _add(ticker="AL30", short_name="AL30", instrument_type="BONAR", sheet="Soberanos",
         isin="ARARGE3209S6",
         raw_fields={"byma": {"tipoEspecie": "Titulos Publicos", "securityType": "GO",
                              "emisor": "Rep. Argentina",
                              "ficha": {"ley": "Extranjera", "monto_residual": 100}}})
    csv = _csv(tmp_path, "AL30;ARARGE3209S6;Titulos Publicos;GO;Rep. Argentina\n")

    enrich_isin_from_byma(csv)

    _, raw = _get("AL30")
    assert "ficha" in raw["byma"], raw["byma"]
    assert raw["byma"]["ficha"]["ley"] == "Extranjera"


def test_enrich_es_idempotente_con_ficha_presente(tmp_db, tmp_path):
    """Con la ficha ya guardada, un 2º enrich no debe reportar cambios (hoy reescribe
    la fila en CADA arranque → re-scrapeo de ~477 fichas por boot)."""
    _add(ticker="AL30", short_name="AL30", instrument_type="BONAR", sheet="Soberanos",
         isin="ARARGE3209S6",
         raw_fields={"byma": {"tipoEspecie": "Titulos Publicos", "securityType": "GO",
                              "emisor": "Rep. Argentina", "ficha": {"ley": "Extranjera"}}})
    csv = _csv(tmp_path, "AL30;ARARGE3209S6;Titulos Publicos;GO;Rep. Argentina\n")

    assert enrich_isin_from_byma(csv) == 0


def test_enrich_no_pisa_con_none_el_isin_cargado_a_mano(tmp_db, tmp_path):
    """Símbolo presente en el CSV pero SIN codigoIsin → no puede borrar el ISIN
    que el ABM guardó en la DB (SQLite es la fuente de verdad)."""
    _add(ticker="ALAAD", short_name="ALAAD", instrument_type="HARD DOLLAR",
         sheet="Obligaciones_Negociables", isin="ARMANUAL00001")
    csv = _csv(tmp_path, "ALAAD;;Obligaciones Negociables;CORP;Aluar\n")

    changed = enrich_isin_from_byma(csv)

    isin, _ = _get("ALAAD")
    assert isin == "ARMANUAL00001"
    assert changed == 1   # sí escribe la metadata byma, pero NO degrada el ISIN


def test_enrich_sin_isin_en_ninguno_de_los_dos_es_idempotente(tmp_db, tmp_path):
    """Fila sin ISIN + semilla sin ISIN: la 2ª corrida no debe reportar cambios."""
    _add(ticker="D30S6", short_name="D30S6", instrument_type="DOLAR_LINKED",
         sheet="Dolar_Linked")
    csv = _csv(tmp_path, "D30S6;;Titulos Publicos;GO;Rep. Argentina\n")

    assert enrich_isin_from_byma(csv) == 1
    assert enrich_isin_from_byma(csv) == 0


def test_enrich_completa_el_isin_faltante(tmp_db, tmp_path):
    """No-regresión: el caso feliz (semilla aporta ISIN, fila sin ISIN) sigue andando."""
    _add(ticker="GD30", short_name="GD30", instrument_type="GLOBAL", sheet="Soberanos")
    csv = _csv(tmp_path, "GD30;ARARGE3209U4;Titulos Publicos;GO;Rep. Argentina\n")

    assert enrich_isin_from_byma(csv) == 1
    isin, raw = _get("GD30")
    assert isin == "ARARGE3209U4"
    assert raw["byma"]["emisor"] == "Rep. Argentina"


def test_enrich_ficha_y_byma_convergen(tmp_db, tmp_path):
    """Cadena real del arranque: enrich_ficha_meta → enrich_isin_from_byma →
    enrich_ficha_meta. La 2ª pasada de ficha NO debe volver a scrapear (hoy sí:
    el enrich del CSV le borró la clave `ficha` en el medio)."""
    from core.infrastructure.byma.catalog_enrich import enrich_ficha_meta

    _add(ticker="AL30", short_name="AL30", instrument_type="BONAR", sheet="Soberanos",
         isin="ARARGE3209S6")
    csv = _csv(tmp_path, "AL30;ARARGE3209S6;Titulos Publicos;GO;Rep. Argentina\n")

    calls = []

    def fake_fetch(sym):
        calls.append(sym)
        return {"ley": "Extranjera", "moneda": "Dolares"}

    assert enrich_ficha_meta(fetch_fn=fake_fetch) == 1
    enrich_isin_from_byma(csv)
    assert enrich_ficha_meta(fetch_fn=fake_fetch) == 0   # ya la tiene → 0 requests
    assert len(calls) == 1, calls
