"""Matcher de calificaciones FIX SCR por emisor (core/infrastructure/ratings.py).

Verifica los casos no triviales (acrónimos, sufijos, "- Clase X") y —clave— que no
haya falsos positivos (TGS no debe matchear TGN; bancos/financieras sin rating)."""

from core.infrastructure.ratings import rating_for


def test_matches_known_emisores():
    assert rating_for("YPF S.A.")["rating"] == "AAA(arg)"
    assert rating_for("ARCOR S.A.I.C.")["rating"] == "AAA(arg)"
    assert rating_for("ARCOR S.A.I.C. - Clase 1")["rating"] == "AAA(arg)"       # strip "- Clase X"
    assert rating_for("COMPAÑIA GENERAL DE COMBUSTIBLES S.A.")["rating"] == "AA-(arg)"
    assert rating_for("LOMA NEGRA COMPAÑIA INDUSTRIAL ARGENTINA SOCIEDAD ANONIMA")["rating"] == "AAA(arg)"
    # acrónimo entre paréntesis (EDENOR) pese a typos/abreviaturas en el nombre
    assert rating_for("EMPRESA DISTRIB. Y COMERCIALZADORA NORTE S.A. (EDENOR S.A.)")["rating"] == "A+(arg)"
    assert rating_for("EDESA S.A.")["rating"] == "A+(arg)"                       # token = acrónimo FIX
    t = rating_for("TELECOM ARGENTINA S. A.")
    assert t["rating"] == "AA+(arg)" and t["perspectiva"] == "Positiva"
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
    assert rating_for("EDESA S.A.")["grade"] == "good"       # A+
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
