"""Sincronización del catálogo de letras contra la API de ArgentinaDatos.

Lo que MIDE la forma de este módulo (verificado contra la API viva el 2026-09-04):

* La API devuelve 18 letras, y de las 16 que ya estaban en el catálogo **ninguna
  difería**: es una buena fuente de `vpv` y vencimiento, que es justamente lo que
  precia una letra (un solo flujo al vencimiento).
* Pero trae `fechaEmision` en **6 de 18** y `tem` en las **mismas 6**. En el resto
  manda `""` y `0`. Eso NO es "emitida en el año 0 con tasa cero": es dato ausente,
  el mismo error que costó fabricar suscripciones fantasma en `fci_history` cuando
  se leyó `ccp<=0` como circulación cero.
* Y lista letras **ya vencidas** (S17A6 y S30A6 vencieron en abril y seguían en el
  payload en septiembre), así que "está en la API" no significa "hay que darla de alta".

De ahí las tres reglas duras: sólo ALTAS (nunca pisar lo que ya está), sólo con dato
completo, y nunca una letra vencida.
"""

from datetime import date

import pytest

from core.infrastructure.letras_sync import Plan, planificar

HOY = date(2026, 9, 4)


def _api(ticker="S30X6", emision="2026-01-30", vto="2026-12-30", tem=2.5, vpv=117.5):
    return {"ticker": ticker, "fechaEmision": emision, "fechaVencimiento": vto,
            "tem": tem, "vpv": vpv}


def _cat(vto="2026-12-30", emi="2026-01-30", pago=117.5):
    return {"vto": vto, "emi": emi, "pago": pago}


# ── altas ────────────────────────────────────────────────────────────────────
def test_una_letra_nueva_y_completa_se_da_de_alta():
    plan = planificar([_api("S30X6")], {}, hoy=HOY)
    assert [a["ticker"] for a in plan.altas] == ["S30X6"]
    assert plan.altas[0]["clase"] == "LECAP"
    assert plan.altas[0]["fecha_pago"] == "2026-12-30"
    assert plan.altas[0]["pago"] == pytest.approx(117.5)


def test_el_prefijo_del_ticker_decide_LECAP_vs_BONCAP():
    """`S…` son LECAP y `T…` BONCAP. El tipo tiene que ser uno de
    `instrument_groups`, si no el bono queda invisible en todos los paneles."""
    plan = planificar([_api("S30X6"), _api("T30X7", vto="2027-12-30")], {}, hoy=HOY)
    clases = {a["ticker"]: a["clase"] for a in plan.altas}
    assert clases == {"S30X6": "LECAP", "T30X7": "BONCAP"}


def test_el_alta_lleva_UN_solo_flujo_igual_al_vpv():
    """Una letra capitalizable paga todo junto al vencimiento. Un alta sin flujos
    la dejaría impriceable, y `save_instrument` la rechaza."""
    plan = planificar([_api(vpv=109.65138)], {}, hoy=HOY)
    cfs = plan.altas[0]["cashflows"]
    assert len(cfs) == 1
    # Las claves son las que parsea el ABM (`date`/`amortization`/`interest`): con
    # los nombres de la tabla, `_parse_cashflows` devuelve [] y el alta se rechaza.
    assert cfs[0]["date"] == "2026-12-30"
    assert cfs[0]["amortization"] == pytest.approx(109.65138)
    assert cfs[0]["interest"] == 0


# ── lo que NO se toca ────────────────────────────────────────────────────────
def test_una_letra_que_ya_esta_NO_se_pisa():
    """El catálogo es la fuente de verdad y sus datos salen de IAMC/BYMA, más ricos
    que los de la API. Una letra existente sólo se REPORTA."""
    plan = planificar([_api("S30X6", tem=9.99, vpv=200.0)],
                      {"S30X6": _cat()}, hoy=HOY)
    assert plan.altas == []
    assert len(plan.diferencias) == 1
    assert plan.diferencias[0]["ticker"] == "S30X6"


def test_una_letra_ya_vencida_no_se_da_de_alta():
    """La API listaba S17A6 (vencida en abril) todavía en septiembre. Darla de alta
    ensucia el catálogo con un bono muerto."""
    plan = planificar([_api("S17A6", vto="2026-04-17")], {}, hoy=HOY)
    assert plan.altas == []
    assert [v["ticker"] for v in plan.vencidas] == ["S17A6"]


def test_el_dia_del_vencimiento_TODAVIA_cuenta():
    """Una letra que vence hoy sigue viva hasta que se paga: el borde es `<`, no `<=`."""
    plan = planificar([_api("S04S6", vto="2026-09-04")], {}, hoy=HOY)
    assert [a["ticker"] for a in plan.altas] == ["S04S6"]


def test_sin_fecha_de_emision_NO_se_inventa_nada():
    """12 de 18 vienen con `fechaEmision: ""`. El ABM la exige y no se puede
    deducir: la letra se reporta para carga manual, no se fabrica una fecha."""
    plan = planificar([_api("T30J7", emision="")], {}, hoy=HOY)
    assert plan.altas == []
    assert [i["ticker"] for i in plan.incompletas] == ["T30J7"]
    assert "emisi" in plan.incompletas[0]["falta"].lower()


def test_un_tem_en_cero_es_dato_AUSENTE_no_una_tasa_de_cero():
    """Mismo error que `ccp<=0` en fci_history. Con `tem=0` el alta sigue (el flujo
    no depende de la tasa) pero el campo se deja VACIO, no en 0,0."""
    plan = planificar([_api(tem=0)], {}, hoy=HOY)
    assert len(plan.altas) == 1
    assert plan.altas[0]["tem_licit"] is None


def test_una_letra_del_catalogo_que_la_API_no_lista_no_se_borra_jamas():
    """La API dejó de listar S12J6 y el catálogo la conserva. Sincronizar en los dos
    sentidos borraría datos por un hueco de la fuente."""
    plan = planificar([], {"S12J6": _cat()}, hoy=HOY)
    assert plan.altas == [] and plan.diferencias == []
    assert plan.solo_en_catalogo == ["S12J6"]


# ── validación del payload ───────────────────────────────────────────────────
@pytest.mark.parametrize("row, motivo", [
    ({"ticker": "", "fechaVencimiento": "2026-12-30", "vpv": 100}, "ticker"),
    ({"ticker": "S30X6", "fechaVencimiento": "", "vpv": 100}, "vencimiento"),
    ({"ticker": "S30X6", "fechaVencimiento": "no-es-fecha", "vpv": 100}, "vencimiento"),
    ({"ticker": "S30X6", "fechaVencimiento": "2026-12-30", "vpv": 0}, "vpv"),
    ({"ticker": "S30X6", "fechaVencimiento": "2026-12-30", "vpv": 99999}, "vpv"),
])
def test_una_fila_rota_se_descarta_con_motivo(row, motivo):
    """Cada fila entra al catálogo, que es la fuente de verdad: se valida en el borde."""
    plan = planificar([{**row, "fechaEmision": "2026-01-30", "tem": 2.5}], {}, hoy=HOY)
    assert plan.altas == []
    assert len(plan.invalidas) == 1
    assert motivo in plan.invalidas[0]["motivo"].lower()


def test_un_ticker_que_no_parece_una_letra_se_descarta():
    """La clase sale del PREFIJO del ticker. Si la API empezara a mezclar otra cosa,
    inventarle LECAP la haría preciar como lo que no es."""
    plan = planificar([_api("AL30")], {}, hoy=HOY)
    assert plan.altas == []
    assert len(plan.invalidas) == 1


def test_un_payload_vacio_no_dispara_nada():
    plan = planificar([], {}, hoy=HOY)
    assert plan.altas == [] and plan.invalidas == []
    assert plan.rechazado is None


def test_un_payload_sospechosamente_chico_se_RECHAZA_entero():
    """Guard del mismo tipo que el de ratings: si la fuente devuelve muchas menos
    letras de las que ya tenemos, es un corte roto, no que se hayan extinguido.
    Sin esto, un mal día de la API se convierte en decisiones sobre el catálogo."""
    catalogo = {f"S{i:02d}X6": _cat() for i in range(1, 18)}     # 17 vivas
    plan = planificar([_api("S30X6")], catalogo, hoy=HOY)        # la API trae 1
    assert plan.rechazado is not None
    assert plan.altas == [] and plan.diferencias == []


def test_el_guard_no_se_activa_con_un_catalogo_chico():
    """Arrancar de cero (catálogo sin letras) no puede parecer un corte roto."""
    plan = planificar([_api("S30X6")], {}, hoy=HOY)
    assert plan.rechazado is None and len(plan.altas) == 1


def test_el_plan_dice_si_hay_algo_que_hacer():
    assert not planificar([], {}, hoy=HOY).hay_altas
    assert planificar([_api()], {}, hoy=HOY).hay_altas


def test_planificar_no_MUTA_lo_que_recibe():
    """El payload viene del cache del provider, compartido entre hilos."""
    fila = _api()
    copia = dict(fila)
    catalogo = {"S99X6": _cat()}
    planificar([fila], catalogo, hoy=HOY)
    assert fila == copia
    assert list(catalogo) == ["S99X6"]


def test_el_plan_se_resume_en_una_linea():
    """Lo lee un log de servidor: si no cabe en una línea, nadie lo mira."""
    plan = planificar([_api("S30X6"), _api("S17A6", vto="2026-04-17"),
                       _api("T30J7", emision="")], {}, hoy=HOY)
    resumen = plan.resumen()
    assert "1" in resumen and "\n" not in resumen
    assert isinstance(plan, Plan)
