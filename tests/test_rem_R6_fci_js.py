"""Remediación lote R6 (FCI) — comportamiento del cliente `apps/web/static/js/fci.js`
sobre node (mismo harness que tests/test_aud_E_fci_js.py, mismo archivo que sirve la app).

Cubre las dos piezas del cliente que quedaron abiertas en la auditoría del lote E:

  1. **Fondos con clases en monedas distintas** (95 de 1.096 en el corte real). El fix
     anterior convertía la serie con la moneda del FONDO, pero el server la entregaba ya
     mergeada entre clases: a un fondo rotulado USD con una clase en pesos se le
     multiplicaba por el MEP plata que ya estaba en pesos. Ahora el server manda las dos
     patas separadas (`flows` / `flows_usd`) y el cliente convierte SOLO la de dólares.
  2. **`PDAYS.ytd`**: mismo bug de parseo UTC que el hallazgo 6 arregló en `monthLabels`,
     dos funciones más abajo.
"""

import pytest

from tests.test_aud_E_fci_js import run_js  # harness compartido (skipea sin node)

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("node") is None, reason="node no disponible")

MEP = 1400.0

# 'Alamerica Renta Fija Argentina' del corte real: rotulado USD (sus clases D/E son en
# dólares) pero su Clase I es en PESOS y aporta +3,1243e9. La pata USD son US$100.000.
_MIXTO = """
function arr(last) { var a = []; for (var i = 0; i < 11; i++) a.push(0); a.push(last); return a; }
api.setM({ macro: { mep: {}, cer: {}, mep_now: %s }, fecha_base: "2026-08-31",
           periods: [], period_labels: {}, cat_order: ["Renta Fija"], subs_by_cat: {},
           hist_axis: [] });
var mixto = { fid: 7, fondo: "Alamerica Renta Fija Argentina", soc: "Alamerica",
              cat: "Renta Fija", sub: "RENTA FIJA USD", moneda: "USD", settle: "T+0",
              flows: arr(3.1243e9), flows_usd: arr(1e5), flows_real: true,
              clases: [], rend: {}, hist: null, aum: null };
"""


def test_solo_la_pata_en_dolares_se_convierte_al_mep(tmp_path):
    """El corazón de la regresión: 3,1243e9 en pesos + US$100.000 al MEP = 3,2643e9.
    Convirtiendo el merge con la moneda del fondo daban 4,374e12 (1.340× de más)."""
    out = run_js(tmp_path, (_MIXTO % MEP) + """
api.setFunds([mixto]);
var fl = api.flowsARS(mixto);
return { last: fl[11], total: fl.reduce(function (a, b) { return a + b; }, 0) };
""")
    assert out["last"] == pytest.approx(3.1243e9 + 1e5 * MEP, rel=1e-9)
    assert out["total"] == pytest.approx(3.1243e9 + 1e5 * MEP, rel=1e-9)
    assert out["last"] < 4e9, "la pata en pesos se multiplicó por el MEP"


def test_la_vista_flujos_agrega_el_fondo_mixto_en_pesos(tmp_path):
    """Agregado del mercado: el fondo mixto entra por 3,26 MM, no por 4,37 B."""
    out = run_js(tmp_path, (_MIXTO % MEP) + """
api.setFunds([mixto]);
api.S.flowCat = "Todas"; api.S.flowWin = 12;
api.renderFlujos();
return api.html("view");
""")
    assert "$3.3 MM" in out, out[-800:]
    assert " B<" not in out and "$4.37 B" not in out


def test_sin_mep_el_fondo_mixto_queda_fuera_del_agregado(tmp_path):
    """Sin MEP no se puede sumar la pata en dólares: el fondo sale del total (con aviso)
    en vez de sumar dólares como si fueran pesos."""
    out = run_js(tmp_path, (_MIXTO % "null") + """
api.setFunds([mixto]);
api.S.flowCat = "Todas"; api.S.flowWin = 12;
api.renderFlujos();
return { html: api.html("view"), fl: api.flowsARS(mixto) };
""")
    assert out["fl"] is None
    assert "sin MEP para convertir" in out["html"]


def test_una_pata_usd_toda_en_cero_no_saca_al_fondo_del_agregado(tmp_path):
    """El fondo tuvo flujo en dólares FUERA de la ventana de 12 meses (el server manda
    `flows_usd` igual): sin nada que convertir, no hay motivo para excluirlo si falta el
    MEP. Antes de este chequeo bastaba la presencia del campo."""
    out = run_js(tmp_path, (_MIXTO % "null") + """
var z = []; for (var j = 0; j < 12; j++) z.push(0);
var f = { fid: 3, fondo: "Con pata USD vacia", soc: "G", cat: "Renta Fija", sub: "S",
          moneda: "ARS", settle: "T+0", flows: arr(7e9), flows_usd: z, flows_real: true,
          clases: [], rend: {}, hist: null, aum: null };
api.setFunds([f]);
return api.flowsARS(f);
""")
    assert out is not None and out[11] == pytest.approx(7e9)


def test_un_fondo_sin_pata_en_dolares_no_necesita_mep(tmp_path):
    """La mayoría (mono-moneda en pesos) no trae `flows_usd`: se agrega tal cual, incluso
    con dolarapi caído."""
    out = run_js(tmp_path, (_MIXTO % "null") + """
var ars = { fid: 1, fondo: "MM Pesos", soc: "G", cat: "Renta Fija", sub: "S",
            moneda: "ARS", settle: "T+0", flows: arr(50e9), flows_real: true,
            clases: [], rend: {}, hist: null, aum: null };
api.setFunds([ars]);
return api.flowsARS(ars);
""")
    assert out[11] == pytest.approx(50e9)


# --------------------------------------------------------------------------- #
# Tab Flujos del detalle: la unidad se elige sin mezclar patas
# --------------------------------------------------------------------------- #
_DETALLE = """
api.setFunds([f]);
api.openDetail(String(f.fid));
api.detail().tab = "flows";
api.drawPane();
return api.html("dpane");
"""


def test_detalle_de_un_fondo_mixto_muestra_pesos_con_la_pata_usd_al_mep(tmp_path):
    out = run_js(tmp_path, (_MIXTO % MEP) + "var f = mixto;" + _DETALLE)
    assert "$3.3 MM" in out
    assert "las clases en dólares, al MEP del corte" in out


def test_detalle_de_un_fondo_mixto_sin_mep_avisa_que_falta_la_pata_usd(tmp_path):
    """Sin MEP se muestra SOLO la pata en pesos, diciéndolo. Antes se dibujaba un número
    que era pesos y dólares sumados sin convertir."""
    out = run_js(tmp_path, (_MIXTO % "null") + "var f = mixto;" + _DETALLE)
    assert "$3.1 MM" in out
    assert "sin MEP no se pueden sumar las clases en dólares" in out


def test_detalle_de_un_fondo_solo_en_dolares_rotula_en_dolares(tmp_path):
    """Sin clases en pesos, el eje queda en su moneda nativa (no se convierte de más)."""
    out = run_js(tmp_path, (_MIXTO % MEP) + """
var z = []; for (var j = 0; j < 12; j++) z.push(0);
var f = { fid: 9, fondo: "MM Dolares", soc: "G", cat: "Renta Fija", sub: "S",
          moneda: "USD", settle: "T+0", flows: z, flows_usd: arr(30e6), flows_real: true,
          clases: [], rend: {}, hist: null, aum: null };
""" + _DETALLE)
    assert "US$30 M" in out
    assert "en dólares (la moneda de sus clases)" in out


# --------------------------------------------------------------------------- #
# PDAYS.ytd: el mismo parseo UTC que el hallazgo 6 arregló en monthLabels
# --------------------------------------------------------------------------- #
_BOOT = """
api.load({ meta: { fecha_base: "%s", periods: [], period_labels: {}, cat_order: [],
                   subs_by_cat: {}, hist_axis: ["2026-01-01"],
                   macro: { mep: {}, cer: {}, mep_now: 1400 },
                   n_total: 0, n_shown: 0, n_aum_real: 0, flows_real: false, sources: {} },
           funds: [] });
return api.getPDAYS().ytd;
"""


def test_pdays_ytd_con_fecha_base_1_de_enero(tmp_path):
    """`new Date("2026-01-01")` es UTC: en ART (UTC−3) se lee como 31/12/2025, así que
    getFullYear() da 2025 y el YTD sale ~365 días en vez de 1. El server computa
    `(base - date(base.year,1,1)).days or 1` = 1 (lens.py). Con 365, el `directo` YTD se
    anualizaba ×365/365 en vez de ×365/1: la TNA del período quedaba 365× baja."""
    assert run_js(tmp_path, _BOOT % "2026-01-01",
                  tz="America/Argentina/Buenos_Aires") == 1


@pytest.mark.parametrize("fecha_base,esperado", [("2026-09-02", 245), ("2026-03-01", 60)])
def test_pdays_ytd_en_el_resto_del_ano_no_cambia(tmp_path, fecha_base, esperado):
    """El camino que ya andaba sigue igual (día del año en fecha civil)."""
    assert run_js(tmp_path, _BOOT % fecha_base,
                  tz="America/Argentina/Buenos_Aires") == esperado


def test_pdays_ytd_sin_fecha_base_cae_al_default(tmp_path):
    assert run_js(tmp_path, _BOOT % "") == 155
