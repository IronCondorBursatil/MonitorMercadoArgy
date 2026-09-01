"""raw_fields del alta masiva IAMC: MERGE, no reemplazo (scripts/ingest_iamc_2026_08.py).

`raw_fields` es un blob COMPARTIDO por varios productores: este script escribe
`origen`/`ley_aplicable`, los scripts de clases escriben `serie_clase`, la semilla
Excel deja `cupon anual %` y el ABM guarda ahi el `sector_override`. Asignarlo
entero (`orm.raw_fields = {...}`) borraba en silencio todo lo que el script no
maneja: re-correr el ingest evaporaba las 182 clases cargadas y el override manual.
"""

from scripts.ingest_iamc_2026_08 import merge_raw_fields


def test_preserva_las_claves_de_otros_productores():
    previo = {
        "serie_clase": "Clase XXXIX (39)",
        "cupon anual %": "8,25",
        "sector_override": "Energía",
    }
    out = merge_raw_fields(previo, "AR")
    assert out["serie_clase"] == "Clase XXXIX (39)"
    assert out["cupon anual %"] == "8,25"
    assert out["sector_override"] == "Energía"


def test_escribe_lo_propio_del_script():
    out = merge_raw_fields({"serie_clase": "Clase IV (4)"}, "Extranjera")
    assert out["ley_aplicable"] == "Extranjera"
    assert "IAMC" in out["origen"]


def test_no_muta_el_dict_original():
    """Devolver un dict NUEVO es lo que hace que SQLAlchemy vea el cambio del JSON
    (y evita que un alias mutado se filtre a otra fila)."""
    previo = {"serie_clase": "Clase V (5)"}
    out = merge_raw_fields(previo, "AR")
    assert previo == {"serie_clase": "Clase V (5)"}
    assert out is not previo


def test_arranca_de_cero_si_no_habia_nada():
    assert merge_raw_fields(None, "AR")["ley_aplicable"] == "AR"


def test_una_ley_ausente_no_pisa_la_que_ya_estaba():
    """El motor elige MEP (ley AR) vs CCL (Extranjera) por este campo: pisarlo con
    None cambiaria el pricing de la pata pesos de las ON hard-dollar."""
    assert merge_raw_fields({"ley_aplicable": "Extranjera"}, None)["ley_aplicable"] \
        == "Extranjera"
    assert merge_raw_fields({"ley_aplicable": "Extranjera"}, "")["ley_aplicable"] \
        == "Extranjera"


def test_es_idempotente():
    previo = {"serie_clase": "Clase XI (11)"}
    una = merge_raw_fields(previo, "AR")
    assert merge_raw_fields(una, "AR") == una
