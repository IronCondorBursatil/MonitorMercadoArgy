"""Auditoría lote E — tests de comportamiento de `apps/web/static/js/fci.js` sobre node.

`fci.js` es un IIFE sin exports: el harness lo carga tal cual (mismo archivo que sirve
la app, sin copiarlo ni reescribirlo), le stubea el DOM/localStorage/fetch y le devuelve
las funciones internas para ejercitarlas. Si `node` no está instalado, los tests se
saltean (el gate de Windows lo tiene; el droplet no corre pytest).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_FCI_JS = _ROOT / "apps" / "web" / "static" / "js" / "fci.js"
_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node no disponible")

_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const OPEN = '(function () {';
let body = src.slice(src.indexOf(OPEN) + OPEN.length);
body = body.slice(0, body.lastIndexOf('})();'));

function El(id) {
  this.id = id; this.innerHTML = ''; this.dataset = {}; this.style = {}; this.value = '';
  this.classList = { toggle: function () {}, add: function () {}, remove: function () {} };
  this.offsetHeight = 0; this.parentNode = this;
}
El.prototype.querySelectorAll = function () { return []; };
El.prototype.addEventListener = function () {};
El.prototype.appendChild = function () {};
El.prototype.click = function () {};
const REG = {};
global.document = {
  getElementById: function (id) { return REG[id] || (REG[id] = new El(id)); },
  querySelectorAll: function () { return []; },
  createElement: function () { return new El('tmp'); },
  addEventListener: function () {},
  documentElement: {}, body: {}
};
global.getComputedStyle = function () { return { getPropertyValue: function () { return ''; } }; };
global.localStorage = { getItem: function () { return null; }, setItem: function () {} };
global.MutationObserver = function () { return { observe: function () {} }; };
global.window = global;
global.fetch = function () { const p = { then: function () { return p; }, catch: function () { return p; } }; return p; };
global.requestAnimationFrame = function () {};
global.alert = function () {};

const api = new Function(body + `
;return {
  getRet: getRet, histLens: histLens, vcpSeries: vcpSeries, vcpWindow: vcpWindow,
  monthLabels: monthLabels, esc: esc, fmtAum: fmtAum, S: S, PERIODS: function(){return PERIODS;},
  flowsARS: flowsARS,
  setM: function (m) { M = m; }, setPDAYS: function (p) { PDAYS = p; },
  getPDAYS: function () { return PDAYS; },
  setFunds: function (fs) { FUNDS = fs; FMAP = {}; fs.forEach(function (f) { FMAP[key(f)] = f; }); },
  load: function (d) { D = d; boot(); },
  html: function (id) { return document.getElementById(id).innerHTML; },
  openDetail: openDetail, drawPane: drawPane, detail: function () { return detailState; },
  renderFlujos: renderFlujos, renderComparar: renderComparar, render: render
};`)();

const out = new Function('api', fs.readFileSync(process.argv[3], 'utf8'))(api);
process.stdout.write(JSON.stringify(out === undefined ? null : out));
"""


def run_js(tmp_path, scenario, tz=None):
    """Corre `scenario` (JS, recibe `api`) contra el fci.js real. Devuelve lo que retorne."""
    h = tmp_path / "harness.js"
    h.write_text(_HARNESS, encoding="utf-8")
    s = tmp_path / "scenario.js"
    s.write_text(scenario, encoding="utf-8")
    env = dict(os.environ)
    if tz:
        env["TZ"] = tz
    # encoding explícito: `text=True` decodifica con el ANSI del SO (cp1252 en Windows) y
    # node escribe UTF-8 → un 'ó' del panel llegaba como 'Ã³' y toda comparación con texto
    # acentuado fallaba aunque el HTML fuera correcto.
    p = subprocess.run([_NODE, str(h), str(_FCI_JS), str(s)], capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


_MACRO = """
api.setM({ macro: { mep: { meses_12: 30.0 }, cer: { meses_12: 100.0 }, mep_now: 1500 },
           fecha_base: "2026-09-02" });
api.setPDAYS({ meses_12: 365 });
var usd = { moneda: "USD", rend: { meses_12: { directo: 2.25, tna: 2.25 } } };
var ars = { moneda: "ARS", rend: { meses_12: { directo: 40.0, tna: 40.0 } } };
"""


def test_usd_lens_does_not_deflate_a_usd_fund(tmp_path):
    """Lente 'USD @MEP': el `directo` de un fondo en dólares YA está en dólares. Restarle
    la devaluación otra vez lo hunde a −21%. Mismo criterio que vcpSeries()."""
    out = run_js(tmp_path, _MACRO + """
return { usd_fund: api.getRet(usd, "meses_12", "directo", "usd"),
         ars_fund: api.getRet(ars, "meses_12", "directo", "usd") };
""")
    assert out["usd_fund"] == pytest.approx(2.25)                    # nominal, sin tocar
    assert out["ars_fund"] == pytest.approx((1.40 / 1.30 - 1) * 100, rel=1e-9)


def test_real_lens_converts_usd_fund_to_pesos_before_deflating(tmp_path):
    """Lente 'Real (CER)': deflactar un retorno en dólares por la inflación EN PESOS es
    mezclar unidades. Primero se pasa a pesos con la devaluación, después se deflacta."""
    out = run_js(tmp_path, _MACRO + """
return { usd_fund: api.getRet(usd, "meses_12", "directo", "real"),
         ars_fund: api.getRet(ars, "meses_12", "directo", "real") };
""")
    assert out["usd_fund"] == pytest.approx((1.0225 * 1.30 / 2.00 - 1) * 100, rel=1e-9)
    assert out["ars_fund"] == pytest.approx((1.40 / 2.00 - 1) * 100, rel=1e-9)


def test_hist_lens_does_not_deflate_a_usd_fund(tmp_path):
    """histLens() (gráfico de Comparar) tenía la misma omisión sobre la serie base-100."""
    out = run_js(tmp_path, _MACRO + """
api.S.lens = "usd";
return { usd_fund: api.histLens([100, 110], usd), ars_fund: api.histLens([100, 110], ars) };
""")
    assert out["usd_fund"] == pytest.approx([100.0, 110.0])
    assert out["ars_fund"] == pytest.approx([100.0, 110.0 / 1.30])


# --------------------------------------------------------------------------- #
# Etiquetas de meses del panel Flujos (bug de parseo UTC vs local)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fecha_base,last", [("2026-09-01", "sep"), ("2026-09-02", "sep"),
                                             ("2026-03-01", "mar"), ("2026-01-01", "ene")])
def test_month_labels_match_the_server_buckets(tmp_path, fecha_base, last):
    """`monthly_net_flows` (server) bucketea con `date.fromisoformat` → el ÚLTIMO bucket
    es el mes de `fecha_base`. `monthLabels` usaba `new Date("YYYY-MM-DD")`, que ES UTC:
    en un browser en ART (UTC−3) el día 1 cae en el mes anterior y TODAS las etiquetas se
    corren un mes respecto de los valores."""
    out = run_js(tmp_path, """
api.setM({ fecha_base: "%s", macro: {} });
return api.monthLabels(12);
""" % fecha_base, tz="America/Argentina/Buenos_Aires")
    assert len(out) == 12
    assert out[-1] == last, out


# --------------------------------------------------------------------------- #
# XSS: texto libre de CAFCI inyectado en innerHTML
# --------------------------------------------------------------------------- #
_XSS = "<img src=x onerror=alert(1)>"


def test_comparar_escapes_horizonte(tmp_path):
    """`row("Horizonte", ...)` de la vista Comparar concatenaba `f.horizonte` crudo dentro
    de `innerHTML`. `horizonte` es texto de CAFCI que llega sin sanitizar
    (cafci_provider → derive → /fci/data → innerHTML) y la app no tiene CSP."""
    out = run_js(tmp_path, """
api.setM({ macro: {}, fecha_base: "2026-09-02" });
var f = { fid: 1, fondo: "F", soc: "S", moneda: "ARS", settle: "T+0", horizonte: %s,
          rend: {}, clases: [], hist: null, aum: null, fee_admin: null, min: null };
api.setFunds([f]);
api.S.cmp = ["1"];
api.renderComparar();
return api.html("view");
""" % json.dumps(_XSS))
    assert _XSS not in out, "horizonte inyectado crudo en innerHTML"
    assert "&lt;img src=x onerror=alert(1)&gt;" in out


def test_strip_escapes_fecha_base(tmp_path):
    """`M.fecha_base` tambien se concatenaba crudo en el strip del header. No es
    explotable hoy (el server lo pasa por `date.fromisoformat` antes de armar el meta,
    ver lens.py/dataset.py), pero escaparlo es gratis y saca el ultimo sink de texto de
    servidor sin esc()."""
    out = run_js(tmp_path, """
api.load({ meta: { fecha_base: %s, periods: [], period_labels: {}, cat_order: [],
                   subs_by_cat: {}, hist_axis: ["2026-01-01"],
                   macro: { mep: {}, cer: {}, mep_now: 1500 },
                   n_total: 0, n_shown: 0, n_aum_real: 0, flows_real: false, sources: {} },
           funds: [] });
return api.html("fci-strip");
""" % json.dumps(_XSS))
    assert _XSS not in out
    assert "&lt;img" in out


def test_no_external_text_field_reaches_html_unescaped():
    """Test ESTRUCTURAL (reemplaza el muestreo de test_fci_js_xss.py): enumera TODAS las
    referencias a campos de texto externo (CAFCI) en fci.js y exige que estén envueltas
    en `esc(...)`. Las excepciones son usos que NO son HTML (comparaciones, claves de
    objeto, nombre de archivo, `fila()` que escapa adentro) y están listadas una por una:
    agregar una inserción cruda nueva rompe el test."""
    import re
    text = _FCI_JS.read_text(encoding="utf-8")
    fields = ("fondo", "soc", "obj", "horizonte", "duration", "moneda_full", "isin",
              "bbg", "inicio", "dep", "clase", "tipo", "sub", "fecha_base")
    # usos que no terminan en innerHTML (cada marker es el fragmento que los identifica)
    allowed_lines = (
        "function fstr(f)",          # string de búsqueda, .toLowerCase()
        "groups[f.sub] = groups[f.sub]",  # clave de objeto
        "label: f.fondo",            # dataset de Chart.js (no es innerHTML)
        'byMgr[x.f.soc',       # clave de objeto
        '(x.f.soc || "\u2014") === name',   # comparación
        "String(M.fecha_base",       # parseo de fecha (baseDate: monthLabels + PDAYS.ytd)
        "+ fila(",                   # fila() escapa adentro (esc(String(v)))
        "f.obj.length",              # .length numérico
        "x.sub === f.sub",           # comparación
        'exportNode(document.getElementById("sheet")',   # nombre de archivo (sanitizado)
    )
    pat = re.compile(r"(?<![\w.$])(?:x\.f|f|c|M)\.(" + "|".join(fields) + r")\b")
    offenders = []
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("//"):
            continue                    # comentario, no codigo
        if any(m in line for m in allowed_lines):
            continue
        for m in pat.finditer(line):
            head = line[:m.start()]
            if head.rstrip().endswith("esc(") or head.rstrip().endswith("esc(String("):
                continue
            if line[m.end():].lstrip().startswith("?"):
                continue            # condicion de ternario: guarda, no inserta el valor
            offenders.append((i, m.group(0), line.strip()[:110]))
    assert not offenders, offenders


# --------------------------------------------------------------------------- #
# MEP hardcodeado en el cliente
# --------------------------------------------------------------------------- #
_VCP_SETUP = """
api.setM({ macro: { mep: { meses_12: 30.0 }, cer: { meses_12: 100.0 }, mep_now: %s },
           fecha_base: "2026-09-02", hist_axis: ["2026-01-01", "2026-09-02"] });
api.S.lens = "usd";
var f = { moneda: "ARS", vcp: 100.0, hist: [100, 110] };
"""


def test_vcp_in_usd_needs_a_real_mep(tmp_path):
    """`mep_now` es None POR CONTRATO cuando no hay MEP (lens.py:50). El front dividía
    igual por un 1255 escrito a mano y rotulaba el eje 'USD (MEP)': con un MEP real de
    1500 la cuotaparte 'en dólares' queda ~19,5% sobrestimada, sin señal en la UI."""
    out = run_js(tmp_path, (_VCP_SETUP % "null") + "return api.vcpSeries(f) || {};")
    assert out.get("data") is None, "dibujó una serie en USD sin MEP"
    assert out.get("err"), "no avisa que no hay MEP"


def test_vcp_in_usd_uses_the_real_mep_when_available(tmp_path):
    """Con MEP real la serie se dibuja igual que siempre (no rompimos el camino bueno)."""
    out = run_js(tmp_path, (_VCP_SETUP % "1500") + "return api.vcpSeries(f);")
    assert out["unit"] == "USD (MEP)"
    assert out["data"][-1] == pytest.approx(100.0 / 1500.0)


def test_no_hardcoded_fx_in_the_client():
    """Ningun FX escrito a mano en el codigo de fci.js: envejece solo (1255 era ~el MEP
    de junio 2026) y miente en silencio."""
    code = [ln for ln in _FCI_JS.read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("//")]
    offenders = [ln.strip() for ln in code if "1255" in ln]
    assert not offenders, offenders


# --------------------------------------------------------------------------- #
# Vista Flujos: pesos y dólares sumados en un mismo escalar
# --------------------------------------------------------------------------- #
# El server manda las patas SEPARADAS por moneda de clase: `flows` en pesos y
# `flows_usd` en dolares (ausente si no hay). Ver tests/test_rem_R6_fci_flows_ccy.py.
_FLOWS_SETUP = """
function flows(last) { var a = []; for (var i = 0; i < 11; i++) a.push(0); a.push(last); return a; }
function zeros() { return flows(0); }
api.setM({ macro: { mep: {}, cer: {}, mep_now: %s }, fecha_base: "2026-09-02",
           cat_order: ["Mercado de Dinero"], subs_by_cat: {}, hist_axis: [] });
api.setFunds([
  { fid: 1, fondo: "MM Pesos", soc: "Gestora", cat: "Mercado de Dinero", moneda: "ARS",
    flows: flows(50e9), flows_real: true },
  { fid: 2, fondo: "MM Dolares", soc: "Gestora", cat: "Mercado de Dinero", moneda: "USD",
    flows: zeros(), flows_usd: flows(30e6), flows_real: true }
]);
api.S.flowCat = "Todas"; api.S.flowWin = 12;
api.renderFlujos();
return api.html("view");
"""


def test_flujos_converts_usd_before_aggregating(tmp_path):
    """La vista Flujos es el ÚNICO lugar del panel donde se suma plata ENTRE fondos, y
    los flujos nacen en la moneda nativa del fondo (Δccp × precio de cuotaparte).
    Sumar USD 30 M como si fueran $30 M los subvalúa ~1.400× y da vuelta el ranking por
    gestora. Con MEP 1400: $50.000 M + USD 30 M ($42.000 M) = $92.000 M, no $50.030 M."""
    out = run_js(tmp_path, _FLOWS_SETUP % "1400")
    assert "$92.0 MM" in out                       # 50e9 + 30e6*1400 = 9,2e10
    assert "$50.0 MM" not in out                   # el agregado crudo (pesos + dolares)


def test_flujos_excludes_usd_funds_when_there_is_no_mep(tmp_path):
    """Sin MEP no se puede convertir: el fondo en dólares queda FUERA del agregado y se
    avisa, en vez de sumarse como si fueran pesos."""
    out = run_js(tmp_path, _FLOWS_SETUP % "null")
    assert "$50.0 MM" in out                       # solo la pata ARS
    assert "sin MEP" in out


def test_detail_flow_chart_labels_the_fund_currency(tmp_path):
    """El tab Flujos del detalle de un fondo en dólares rotulaba sus barras con '$'."""
    out = run_js(tmp_path, """
api.setM({ macro: { mep: {}, cer: {}, mep_now: 1400 }, fecha_base: "2026-09-02",
           periods: [], period_labels: {}, cat_order: [], subs_by_cat: {}, hist_axis: [] });
var fl = []; for (var i = 0; i < 11; i++) fl.push(0); fl.push(30e6);
var z = []; for (var j = 0; j < 12; j++) z.push(0);
api.setFunds([{ fid: 2, fondo: "MM Dolares", soc: "G", cat: "Mercado de Dinero", sub: "S",
                moneda: "USD", settle: "T+0", flows: z, flows_usd: fl, flows_real: true,
                clases: [], rend: {}, hist: null, aum: null }]);
api.openDetail("2");
api.detail().tab = "flows";
api.drawPane();
return api.html("dpane");
""")
    assert "US$30 M" in out, out[:400]


def test_real_lens_on_a_usd_fund_is_not_dollars_deflated_by_peso_cpi(tmp_path):
    """Misma mezcla de unidades del hallazgo 2, en el tercer camino: `vcpSeries` con el
    lente 'Real (CER)' sobre un fondo en dólares dividía la serie EN DÓLARES por la
    inflación en pesos y la rotulaba 'USD real (CER)'. Hay que pasarla a pesos con el FX
    y recién ahí deflactar (queda en pesos constantes, como para los fondos en ARS)."""
    out = run_js(tmp_path, """
api.setM({ macro: { mep: { meses_12: 30.0 }, cer: { meses_12: 100.0 }, mep_now: 1500 },
           fecha_base: "2026-09-02", hist_axis: ["2026-01-01", "2026-09-02"] });
api.S.lens = "real";
return api.vcpSeries({ moneda: "USD", vcp: 100.0, hist: [100, 100] });
""")
    assert out["unit"] == "$ real (CER)"
    # t=1 (hoy): 100 USD × 1500 $/USD / (1 + 100%) = 75.000 pesos constantes
    assert out["data"][-1] == pytest.approx(100.0 * 1500.0 / 2.0)
