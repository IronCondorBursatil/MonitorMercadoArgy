/* Panel FCI — app cliente (vanilla). Lee /fci/data (dataset armado en server desde
   CAFCI + ArgentinaDatos + fci_history + lente A3500/CER). Theme Balanz vía app.css. */
(function () {
"use strict";

var D = null, M = null, FUNDS = null, FMAP = {};
var PERIODS = [], PL = {}, PDAYS = {};
var COLORS = ["#3a5fcf", "#0d6e3a", "#a8242b", "#b9831b", "#6b3fa0", "#0f7a8a", "#c2417f", "#5a6b1e"];

var S = {
  view: "mercado", cat: "Mercado de Dinero", q: "",
  lens: "ars", metric: "tna", period: "mes_1",
  rkCat: "Mercado de Dinero", rkPeriod: "mes_1",
  flowCat: "Todas", flowWin: 12, flowOpen: {},
  collapsed: {},
  favs: new Set(JSON.parse(localStorage.getItem("fci_favs") || "[]")),
  cmp: JSON.parse(localStorage.getItem("fci_cmp") || "[]")
};
function saveFavs() { localStorage.setItem("fci_favs", JSON.stringify([].concat(Array.from(S.favs)))); }
function saveCmp() { localStorage.setItem("fci_cmp", JSON.stringify(S.cmp)); }

var key = function (f) { return String(f.fid); };

// ---------------- formatters ----------------
function fmtPct(v, dec) { if (v == null || isNaN(v)) return "—"; return v.toFixed(dec == null ? 1 : dec) + "%"; }
function cls(v) { return v == null ? "" : (v >= 0 ? "pos" : "neg"); }
function fmtAum(v) {
  if (v == null) return "—";
  var a = Math.abs(v), sg = v < 0 ? "-" : "";
  if (a >= 1e12) return sg + "$" + (a / 1e12).toFixed(2) + " B";
  if (a >= 1e9) return sg + "$" + (a / 1e9).toFixed(1) + " MM";
  if (a >= 1e6) return sg + "$" + (a / 1e6).toFixed(0) + " M";
  return sg + "$" + Math.round(a).toLocaleString("es-AR");
}
function fmtMoney(v) { if (v == null) return "—"; return "$" + Number(v).toLocaleString("es-AR"); }
function esc(s) {
  if (s == null) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

// lens-adjusted return for a fund/period/metric
function getRet(f, period, metric, lens) {
  var r = (f.rend || {})[period] || {};
  var d = r.directo;
  if (d == null) return null;
  if (lens !== "ars") {
    var mkey = lens === "usd" ? "mep" : (lens === "real" ? "cer" : null);
    var macro = (mkey && (M.macro[mkey] || {})[period]) || 0;
    d = ((1 + d / 100) / (1 + macro / 100) - 1) * 100;
    if (metric === "tna") return d / (PDAYS[period] || 365) * 365;
    return d;
  }
  return metric === "tna" ? r.tna : d;
}
function lensTag() { return S.lens === "usd" ? " · USD @MEP" : (S.lens === "real" ? " · real s/CER" : ""); }
// deflacta una serie base-100 según el lens (rampa lineal a 12m)
function histLens(series) {
  if (!series || S.lens === "ars") return series;
  var mkey = S.lens === "usd" ? "mep" : "cer";
  var m = (M.macro[mkey] || {}).meses_12 || 0, n = series.length;
  return series.map(function (v, i) { return v / (1 + m / 100 * (n > 1 ? i / (n - 1) : 0)); });
}
// serie de VCP reconstruida en la unidad del lens (ars nominal / usd@mep / real cer)
function vcpSeries(f) {
  if (!f.hist || f.vcp == null) return null;
  var hist = f.hist, n = hist.length, last = hist[n - 1] || 100;
  var nom = hist.map(function (h) { return f.vcp * h / last; });
  if (S.lens === "usd" && f.moneda !== "USD") {
    var m = (M.macro.mep || {}).meses_12 || 0, now = (M.macro.mep_now) || 1255;
    return { unit: "USD (MEP)", data: nom.map(function (v, i) {
      var mep = now / (1 + m / 100 * (1 - (n > 1 ? i / (n - 1) : 0))); return v / mep; }) };
  }
  if (S.lens === "real") {
    var c = (M.macro.cer || {}).meses_12 || 0;
    return { unit: (f.moneda === "USD" ? "USD" : "$") + " real (CER)",
      data: nom.map(function (v, i) { return v / (1 + c / 100 * (n > 1 ? i / (n - 1) : 0)); }) };
  }
  return { unit: f.moneda === "USD" ? "USD" : "ARS", data: nom };
}
function vcpRangeFrom(range) {
  var axis = M.hist_axis, last = axis[axis.length - 1];
  if (range === "max") return axis[0];
  if (range === "ytd") return last.slice(0, 4) + "-01-01";
  var d = new Date(last);
  if (range === "7d") d.setDate(d.getDate() - 7);
  else if (range === "1m") d.setMonth(d.getMonth() - 1);
  else if (range === "3m") d.setMonth(d.getMonth() - 3);
  else if (range === "6m") d.setMonth(d.getMonth() - 6);
  else return axis[0];
  return d.toISOString().slice(0, 10);
}
function vcpWindow(f) {
  var s = vcpSeries(f); if (!s) return null;
  var axis = M.hist_axis, from = detailState.vcpFrom || axis[0], to = detailState.vcpTo || axis[axis.length - 1];
  var lab = [], dat = [];
  for (var i = 0; i < axis.length; i++) { if (axis[i] >= from && axis[i] <= to) { lab.push(axis[i]); dat.push(s.data[i]); } }
  return { unit: s.unit, labels: lab, data: dat, from: from, to: to };
}

function median(arr) {
  if (!arr.length) return null;
  var a = arr.slice().sort(function (x, y) { return x - y; });
  var m = Math.floor(a.length / 2);
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
}
function fstr(f) { return (f.fondo + " " + (f.soc || "") + " " + (f.clases ? f.clases.map(function (c) { return c.clase; }).join(" ") : "")).toLowerCase(); }
function matchq(f) { if (!S.q) return true; return fstr(f).indexOf(S.q.toLowerCase()) >= 0; }

// ---------------- strip + tabs ----------------
function renderStrip() {
  var el = document.getElementById("fci-strip"); if (!el) return;
  var cov = (M.macro.cer && M.macro.cer.meses_12 != null) ? "" :
    "<span class='hs'>lente 12m <span>acumulando</span></span>";
  el.innerHTML =
    "<b>FCI · CAFCI</b><span class='hs'>corte <span>" + (M.fecha_base || "—") + "</span></span>"
    + "<span class='hs'>fondos <span>" + M.n_total + "</span></span>"
    + "<span class='hs'>AUM real <span>" + M.n_aum_real + "/" + M.n_shown + "</span></span>"
    + "<span class='hs'>flujos <span>" + (M.flows_real ? "reales" : "acumulando") + "</span></span>"
    + cov;
}

var VIEWS = [["mercado", "Mercado"], ["rankings", "Rankings"], ["comparar", "Comparar"], ["favoritos", "Favoritos"], ["flujos", "Flujos"]];
function renderTabs() {
  document.getElementById("vtabs").innerHTML = VIEWS.map(function (v) {
    var n = v[0] === "favoritos" ? " (" + S.favs.size + ")" : (v[0] === "comparar" ? " (" + S.cmp.length + ")" : "");
    return "<button class='vtab" + (S.view === v[0] ? " on" : "") + "' data-v='" + v[0] + "'>" + v[1] + n + "</button>";
  }).join("") + "<button class='btn-ghost' id='expview' style='margin-left:auto;align-self:center'>⬇ Imagen</button>";
  document.querySelectorAll("#vtabs .vtab").forEach(function (b) {
    b.onclick = function () { closeModal(); S.view = b.dataset.v; render(); };
  });
  document.getElementById("expview").onclick = function () { exportNode(document.getElementById("view"), "fci_" + S.view); };
}

function toolbarHTML(opts) {
  opts = opts || {};
  var per = opts.period || S.period;
  return "<div class='fci-toolbar'>"
    + (opts.search !== false ? "<input type='search' id='q' placeholder='Buscar fondo o gestora…' value='" + S.q.replace(/'/g, "&#39;") + "'>" : "")
    + (opts.lens !== false ? "<span class='lbl'>Moneda</span><span class='seg' id='seg-lens'>"
        + "<button data-l='ars' class='" + (S.lens === "ars" ? "on" : "") + "'>ARS</button>"
        + "<button data-l='usd' class='" + (S.lens === "usd" ? "on" : "") + "'>USD @MEP</button>"
        + "<button data-l='real' class='" + (S.lens === "real" ? "on" : "") + "'>Real (CER)</button></span>" : "")
    + (opts.metric !== false ? "<span class='lbl'>Métrica</span><span class='seg' id='seg-metric'>"
        + "<button data-m='tna' class='" + (S.metric === "tna" ? "on" : "") + "'>TNA</button>"
        + "<button data-m='directo' class='" + (S.metric === "directo" ? "on" : "") + "'>Directo</button></span>" : "")
    + (opts.period !== false ? "<span class='lbl'>Período</span><span class='seg' id='seg-period'>"
        + PERIODS.map(function (p) { return "<button data-p='" + p + "' class='" + (per === p ? "on" : "") + "'>" + PL[p] + "</button>"; }).join("")
        + "</span>" : "")
    + "</div>";
}
function bindSeg(id, attr, cb) {
  var el = document.getElementById(id); if (!el) return;
  el.querySelectorAll("button").forEach(function (b) { b.onclick = function () { cb(b.dataset[attr]); }; });
}
function wireToolbar(onPeriod) {
  var q = document.getElementById("q");
  if (q) q.oninput = function () { S.q = q.value; renderBody(); };
  bindSeg("seg-lens", "l", function (v) { S.lens = v; render(); });
  bindSeg("seg-metric", "m", function (v) { S.metric = v; render(); });
  bindSeg("seg-period", "p", function (v) { if (onPeriod) onPeriod(v); else { S.period = v; render(); } });
}

// =====================================================================
//  VIEW: MERCADO
// =====================================================================
function renderMercado() {
  var cats = M.cat_order.slice();
  var counts = {}; FUNDS.forEach(function (f) { counts[f.cat] = (counts[f.cat] || 0) + 1; });
  var pills = "<div class='pills'>" + ["Todos"].concat(cats).map(function (c) {
    var n = c === "Todos" ? FUNDS.length : (counts[c] || 0);
    if (!n && c !== "Todos") return "";
    return "<button class='pill" + (S.cat === c ? " on" : "") + "' data-c='" + c + "'>" + c + " <small>" + n + "</small></button>";
  }).join("") + "</div>";
  document.getElementById("view").innerHTML =
    "<section class='panel'><div class='panel-head'><span>FONDOS por subcategoría" + lensTag() + "</span></div>"
    + "<div style='padding:10px 13px 0'>" + toolbarHTML() + pills + "</div><div id='mbody'></div></section>";
  wireToolbar();
  document.querySelectorAll("#view .pill").forEach(function (b) {
    b.onclick = function () {
      S.cat = b.dataset.c;
      document.querySelectorAll("#view .pill").forEach(function (p) { p.classList.toggle("on", p === b); });
      renderBody();
    };
  });
  renderBody();
}
function renderBody() {
  var box = document.getElementById("mbody"); if (!box) return;
  var rows = FUNDS.filter(function (f) { return (S.cat === "Todos" || f.cat === S.cat) && matchq(f); });
  var groups = {}; rows.forEach(function (f) { (groups[f.sub] = groups[f.sub] || []).push(f); });
  var order = [];
  M.cat_order.forEach(function (c) { (M.subs_by_cat[c] || []).forEach(function (s) { if (groups[s]) order.push(s); }); });
  Object.keys(groups).forEach(function (s) { if (order.indexOf(s) < 0) order.push(s); });

  var head = "<table><thead><tr><th></th><th style='text-align:left'>Fondo / Clase</th>"
    + "<th class='opt' style='text-align:left'>Gestora</th><th>Mon</th><th class='opt'>Liq</th>"
    + "<th>" + (S.metric === "tna" ? "TNA" : "Directo") + " " + PL[S.period] + "</th>"
    + PERIODS.map(function (p) { return "<th class='opt'>" + PL[p] + "</th>"; }).join("")
    + "<th>AUM</th><th class='opt'>Costo</th></tr></thead><tbody>";
  var body = "";
  order.forEach(function (sub) {
    var g = groups[sub]; if (!g) return;
    g.sort(function (a, b) {
      var x = getRet(a, S.period, S.metric, S.lens), y = getRet(b, S.period, S.metric, S.lens);
      return (y == null ? -1e9 : y) - (x == null ? -1e9 : x);
    });
    var med = median(g.map(function (f) { return getRet(f, S.period, S.metric, S.lens); }).filter(function (v) { return v != null; }));
    var col = S.collapsed[sub];
    body += "<tr class='grp' data-s='" + esc(sub) + "'><td colspan='" + (6 + PERIODS.length + 2) + "'>"
      + (col ? "▸ " : "▾ ") + esc(sub) + " <span class='gcount'>· " + g.length + " fondos</span>"
      + "<span class='gmed'>· mediana " + (S.metric === "tna" ? "TNA" : "") + " " + fmtPct(med) + "</span></td></tr>";
    if (col) return;
    var max = Math.max.apply(null, g.map(function (f) { var v = getRet(f, S.period, S.metric, S.lens); return v == null ? 0 : v; }).concat([0.01]));
    g.forEach(function (f) {
      var v = getRet(f, S.period, S.metric, S.lens);
      var pct = v == null ? 0 : Math.max(0, Math.min(100, v / max * 100));
      var fav = S.favs.has(key(f));
      body += "<tr class='fund' data-k='" + key(f) + "'>"
        + "<td class='star" + (fav ? " on" : "") + "' data-fav='" + key(f) + "'>" + (fav ? "★" : "☆") + "</td>"
        + "<td><div class='fname'>" + esc(f.fondo) + "</div>" + (f.n_clases > 1 ? "<div class='fsub'>" + f.n_clases + " clases</div>" : "") + "</td>"
        + "<td class='opt' style='font-size:11.5px;color:var(--text-dim)'>" + esc(f.soc || "—") + "</td>"
        + "<td class='num'>" + f.moneda + "</td><td class='opt num'>" + f.settle + "</td>"
        + "<td><div class='bar'><i style='width:" + pct + "%'></i><span class='" + cls(v) + "'>" + fmtPct(v) + "</span></div></td>"
        + PERIODS.map(function (p) { var pv = getRet(f, p, S.metric, S.lens); return "<td class='opt num " + cls(pv) + "'>" + fmtPct(pv) + "</td>"; }).join("")
        + "<td class='num' title='" + (f.aum_real ? "real (ArgentinaDatos)" : "sin dato") + "'>" + fmtAum(f.aum) + (f.aum_real ? "" : " *") + "</td>"
        + "<td class='opt num'>" + fmtPct(f.fee_admin, 2) + "</td></tr>";
    });
  });
  if (!order.length) body = "<tr class='empty'><td colspan='14'>Sin fondos para el filtro</td></tr>";
  box.innerHTML = head + body + "</tbody></table>";
  box.querySelectorAll("tr.grp").forEach(function (r) { r.onclick = function () { S.collapsed[r.dataset.s] = !S.collapsed[r.dataset.s]; renderBody(); }; });
  box.querySelectorAll("td.star").forEach(function (td) { td.onclick = function (e) { e.stopPropagation(); toggleFav(td.dataset.fav); }; });
  box.querySelectorAll("tr.fund").forEach(function (r) { r.onclick = function () { openDetail(r.dataset.k); }; });
}
function toggleFav(k) { if (S.favs.has(k)) S.favs.delete(k); else S.favs.add(k); saveFavs(); renderTabs(); renderBody(); }

// =====================================================================
//  VIEW: RANKINGS
// =====================================================================
function renderRankings() {
  var cats = M.cat_order.filter(function (c) { return c !== "Otros"; });
  var pills = "<div class='pills'>" + cats.map(function (c) {
    return "<button class='pill" + (S.rkCat === c ? " on" : "") + "' data-c='" + c + "'>" + c + "</button>"; }).join("") + "</div>";
  document.getElementById("view").innerHTML =
    "<section class='panel'><div class='panel-head'><span>Ranking · " + S.rkCat + " · " + (S.metric === "tna" ? "TNA" : "Directo") + " " + PL[S.rkPeriod] + lensTag() + "</span></div>"
    + "<div style='padding:10px 13px 0'>" + toolbarHTML({ period: S.rkPeriod }) + pills + "</div>"
    + "<div id='rkbody' style='padding:6px 10px 14px'></div></section>";
  wireToolbar(function (p) { S.rkPeriod = p; renderRankings(); });
  document.querySelectorAll("#view .pill").forEach(function (b) { b.onclick = function () { S.rkCat = b.dataset.c; renderRankings(); }; });
  var g = FUNDS.filter(function (f) { return f.cat === S.rkCat && matchq(f) && getRet(f, S.rkPeriod, S.metric, S.lens) != null; });
  g.sort(function (a, b) { return getRet(b, S.rkPeriod, S.metric, S.lens) - getRet(a, S.rkPeriod, S.metric, S.lens); });
  g = g.slice(0, 25);
  var amax = Math.max.apply(null, g.map(function (f) { return Math.abs(getRet(f, S.rkPeriod, S.metric, S.lens) || 0); }).concat([0.01]));
  document.getElementById("rkbody").innerHTML = g.map(function (f, i) {
    var v = getRet(f, S.rkPeriod, S.metric, S.lens);
    var w = Math.max(3, Math.abs(v) / amax * 100);
    return "<div class='rk" + (i < 3 ? " top" + (i + 1) : "") + "' data-k='" + key(f) + "' style='cursor:pointer'>"
      + "<div class='pos-n'>" + (i + 1) + "</div>"
      + "<div class='rkbar'><i style='width:" + w + "%'></i><b>" + esc(f.fondo) + " <span class='muted'>· " + esc(f.soc || "") + "</span></b></div>"
      + "<div class='rkv " + cls(v) + "'>" + fmtPct(v) + "</div></div>";
  }).join("") || "<p class='empty'>Sin datos</p>";
  document.querySelectorAll("#rkbody .rk").forEach(function (r) { r.onclick = function () { openDetail(r.dataset.k); }; });
}

// =====================================================================
//  VIEW: COMPARAR
// =====================================================================
var cmpChart = null;
function renderComparar() {
  var chosen = S.cmp.map(function (k) { return FMAP[k]; }).filter(Boolean);
  var html = "<section class='panel'><div class='panel-head'><span>Comparar fondos · sin límite" + lensTag() + "</span></div><div style='padding:12px 14px'>";
  html += "<input type='search' id='cmpq' placeholder='Buscar y agregar fondo…' style='min-width:280px'><div id='cmpsug'></div>";
  html += "<div class='chips' id='cmpchips'></div>";
  html += "<div style='margin:6px 0 12px'>" + toolbarHTML({ search: false, period: false }) + "</div>";
  if (chosen.length) {
    html += "<div class='chartbox' style='height:260px'><canvas id='cmpchart'></canvas></div>";
    html += "<p class='note'>Crecimiento de $10.000 (histórico reconstruido de los retornos reales)" + lensTag() + ".</p>";
    html += "<div style='overflow:auto'><table class='cmp-tbl'><colgroup><col style='width:150px'>"
      + chosen.map(function () { return "<col>"; }).join("") + "</colgroup><thead><tr><th style='text-align:left'>Métrica</th>"
      + chosen.map(function (f) { return "<th style='text-align:center'>" + esc(f.fondo) + "</th>"; }).join("") + "</tr></thead><tbody>";
    var row = function (label, fn) { html += "<tr><td style='text-align:left'>" + label + "</td>" + chosen.map(function (f) { return "<td style='text-align:center'>" + fn(f) + "</td>"; }).join("") + "</tr>"; };
    PERIODS.forEach(function (p) { row((S.metric === "tna" ? "TNA " : "Dir ") + PL[p], function (f) { var v = getRet(f, p, S.metric, S.lens); return "<span class='" + cls(v) + "'>" + fmtPct(v) + "</span>"; }); });
    row("AUM", function (f) { return fmtAum(f.aum); });
    row("Costo admin.", function (f) { return fmtPct(f.fee_admin, 2); });
    row("Salida", function (f) { return f.fee_out != null ? fmtPct(f.fee_out, 2) : "—"; });
    row("Liquidación", function (f) { return f.settle; });
    row("Horizonte", function (f) { return f.horizonte || "—"; });
    row("Mín.", function (f) { return f.min ? fmtMoney(f.min) : "—"; });
    html += "</tbody></table></div>";
  } else { html += "<p class='empty'>Agregá fondos para compararlos lado a lado.</p>"; }
  html += "</div></section>";
  document.getElementById("view").innerHTML = html;
  wireToolbar();
  var q = document.getElementById("cmpq");
  q.oninput = function () {
    var v = q.value.toLowerCase(), sug = document.getElementById("cmpsug");
    if (!v) { sug.innerHTML = ""; return; }
    var hits = FUNDS.filter(function (f) { return fstr(f).indexOf(v) >= 0; }).slice(0, 8);
    sug.innerHTML = "<div style='border:1px solid var(--panel-border);border-radius:6px;margin:4px 0'>"
      + hits.map(function (f) { return "<div class='cfg-item' data-k='" + key(f) + "' style='padding:6px 10px;cursor:pointer'>" + esc(f.fondo) + " <span class='muted'>· " + esc(f.soc || "") + "</span></div>"; }).join("") + "</div>";
    sug.querySelectorAll(".cfg-item").forEach(function (d) { d.onclick = function () { if (S.cmp.indexOf(d.dataset.k) < 0) S.cmp.push(d.dataset.k); saveCmp(); q.value = ""; sug.innerHTML = ""; renderTabs(); renderComparar(); }; });
  };
  renderCmpChips();
  if (chosen.length) drawCmpChart(chosen);
}
function renderCmpChips() {
  var box = document.getElementById("cmpchips"); if (!box) return;
  box.innerHTML = S.cmp.map(function (k) { var f = FMAP[k]; if (!f) return ""; return "<span class='chip'>" + esc(f.fondo) + "<button data-k='" + k + "'>×</button></span>"; }).join("");
  box.querySelectorAll("button").forEach(function (b) { b.onclick = function () { S.cmp = S.cmp.filter(function (x) { return x !== b.dataset.k; }); saveCmp(); renderTabs(); renderComparar(); }; });
}
function drawCmpChart(chosen) {
  var ctx = document.getElementById("cmpchart"); if (!ctx || !window.Chart) return;
  if (cmpChart) cmpChart.destroy();
  cmpChart = new Chart(ctx, { type: "line", data: { labels: M.hist_axis, datasets: chosen.map(function (f, i) {
    return { label: f.fondo, data: histLens(f.hist || []).map(function (v) { return v / 100 * 10000; }),
      borderColor: COLORS[i % COLORS.length], backgroundColor: "transparent", borderWidth: 2, pointRadius: 0, tension: .25 };
  }) }, options: chartOpts({ money: true }) });
}

// =====================================================================
//  VIEW: FAVORITOS
// =====================================================================
function renderFavoritos() {
  document.getElementById("view").innerHTML =
    "<section class='panel'><div class='panel-head'><span>Mi watchlist · " + S.favs.size + " fondos" + lensTag() + "</span></div>"
    + "<div style='padding:10px 13px'>" + toolbarHTML() + "<div id='favbody'></div></div></section>";
  wireToolbar();
  var q = document.getElementById("q"); if (q) q.oninput = function () { S.q = q.value; renderFavBody(); };
  renderFavBody();
}
function renderFavBody() {
  var box = document.getElementById("favbody"); if (!box) return;
  var favs = Array.from(S.favs).map(function (k) { return FMAP[k]; }).filter(Boolean).filter(matchq);
  if (!favs.length) {
    box.innerHTML = "<p class='empty'>" + (S.favs.size ? "Ningún favorito coincide con la búsqueda." : "Marcá fondos con ★ en Mercado o Rankings para seguirlos acá.") + "</p>";
    return;
  }
  favs.sort(function (a, b) { return (getRet(b, S.period, S.metric, S.lens) || -1e9) - (getRet(a, S.period, S.metric, S.lens) || -1e9); });
  var html = "<table><thead><tr><th></th><th style='text-align:left'>Fondo</th><th class='opt' style='text-align:left'>Subcat</th><th>Mon</th>"
    + PERIODS.map(function (p) { return "<th>" + PL[p] + "</th>"; }).join("") + "<th>AUM</th></tr></thead><tbody>";
  favs.forEach(function (f) {
    html += "<tr class='fund' data-k='" + key(f) + "'><td class='star on' data-fav='" + key(f) + "'>★</td>"
      + "<td><div class='fname'>" + esc(f.fondo) + "</div><div class='fsub'>" + esc(f.soc || "") + "</div></td>"
      + "<td class='opt' style='font-size:11px;color:var(--text-dim)'>" + esc(f.sub) + "</td><td class='num'>" + esc(f.moneda) + "</td>"
      + PERIODS.map(function (p) { var v = getRet(f, p, S.metric, S.lens); return "<td class='num " + cls(v) + "'>" + fmtPct(v) + "</td>"; }).join("")
      + "<td class='num'>" + fmtAum(f.aum) + "</td></tr>";
  });
  box.innerHTML = html + "</tbody></table>";
  box.querySelectorAll("td.star").forEach(function (td) { td.onclick = function (e) { e.stopPropagation(); toggleFav(td.dataset.fav); renderFavBody(); }; });
  box.querySelectorAll("tr.fund").forEach(function (r) { r.onclick = function () { openDetail(r.dataset.k); }; });
}

// =====================================================================
//  VIEW: FLUJOS
// =====================================================================
function renderFlujos() {
  var cats = ["Todas"].concat(M.cat_order);
  var W = S.flowWin || 12, NALL = 12, start = Math.max(0, NALL - W);
  var pills = "<div class='pills'>" + cats.map(function (c) { return "<button class='pill" + (S.flowCat === c ? " on" : "") + "' data-c='" + c + "'>" + c + "</button>"; }).join("") + "</div>";
  var winSel = "<span class='seg' id='flowwin'>" + [[3, "3M"], [6, "6M"], [12, "12M"]].map(function (w) {
    return "<button data-w='" + w[0] + "' class='" + (W === w[0] ? "on" : "") + "'>" + w[1] + "</button>"; }).join("") + "</span>";
  var mem = (S.flowCat === "Todas") ? FUNDS.slice() : FUNDS.filter(function (f) { return f.cat === S.flowCat; });
  var anyReal = mem.some(function (f) { return f.flows_real; });

  var html = "<section class='panel'><div class='panel-head'><span>Flujos netos · " + S.flowCat + " · últimos " + W + " meses</span></div><div style='padding:12px 14px'>"
    + "<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px'>" + pills
    + "<span class='lbl' style='margin-left:auto'>Plazo</span>" + winSel + "</div>";

  if (!anyReal) {
    html += "<p class='empty'>Flujos en acumulación. El histórico de cuotapartes se está juntando a diario "
      + "(flujo = Δcuotapartes × VCP); en unas ruedas vas a ver los flujos reales por fondo y gestora.</p></div></section>";
    document.getElementById("view").innerHTML = html;
    document.querySelectorAll("#view .pill").forEach(function (b) { b.onclick = function () { S.flowCat = b.dataset.c; renderFlujos(); }; });
    var fw0 = document.getElementById("flowwin");
    if (fw0) fw0.querySelectorAll("button").forEach(function (b) { b.onclick = function () { S.flowWin = parseInt(b.dataset.w, 10); renderFlujos(); }; });
    return;
  }

  var totals = [];
  for (var i = start; i < NALL; i++) { var s = 0; mem.forEach(function (f) { s += (f.flows || [])[i] || 0; }); totals.push(s); }
  var months = monthLabels(W);
  var maxAbs = Math.max.apply(null, totals.map(Math.abs).concat([1]));
  var periodTotal = totals.reduce(function (a, b) { return a + b; }, 0);
  var byMgr = {};
  mem.forEach(function (f) { var sum = 0; for (var i = start; i < NALL; i++) sum += (f.flows || [])[i] || 0; byMgr[f.soc || "—"] = (byMgr[f.soc || "—"] || 0) + sum; });
  var mgrs = Object.keys(byMgr).map(function (k) { return [k, byMgr[k]]; }).sort(function (a, b) { return Math.abs(b[1]) - Math.abs(a[1]); });
  var mgrMax = Math.max.apply(null, mgrs.map(function (m) { return Math.abs(m[1]); }).concat([1]));

  html += "<div class='flowbars'>" + totals.map(function (v, i) {
    var h = Math.abs(v) / maxAbs * 100;
    return "<div class='fb'><div class='b " + (v >= 0 ? "pos" : "neg") + "' style='height:" + h + "%' title='" + months[i] + ": " + fmtAum(v) + "'></div><small>" + months[i] + "</small></div>";
  }).join("") + "</div>"
    + "<p class='note'>Flujo neto mensual (suscripciones − rescates), Δcuotapartes × VCP · acumulado del período: <b class='" + cls(periodTotal) + "'>" + fmtAum(periodTotal) + "</b>.</p>"
    + "<h4 style='margin:16px 0 6px;color:var(--accent);font-size:12px;text-transform:uppercase;letter-spacing:.4px'>Flujos por gestora (" + mgrs.length + ") · acumulado " + W + "M</h4>";
  html += mgrs.map(function (m, mi) {
    var name = m[0], val = m[1], w = Math.abs(val) / mgrMax * 100, open = !!S.flowOpen[name];
    var rowh = "<div class='rk mgr-row' data-mi='" + mi + "' style='cursor:pointer;user-select:none'>"
      + "<div style='width:16px;text-align:center;color:var(--text-faint)'>" + (open ? "▾" : "▸") + "</div>"
      + "<div class='rkbar' style='background:var(--row-alt)'><i style='width:" + w + "%;background:" + (val >= 0 ? "var(--pos)" : "var(--neg)") + ";opacity:.5'></i><b>" + esc(name) + "</b></div>"
      + "<div class='rkv " + (val >= 0 ? "pos" : "neg") + "'>" + fmtAum(val) + "</div></div>";
    if (open) {
      var funds = mem.filter(function (f) { return (f.soc || "—") === name; }).map(function (f) {
        var sum = 0; for (var i = start; i < NALL; i++) sum += (f.flows || [])[i] || 0; return { f: f, v: sum };
      }).sort(function (a, b) { return Math.abs(b.v) - Math.abs(a.v); });
      var fmax = Math.max.apply(null, funds.map(function (x) { return Math.abs(x.v); }).concat([1]));
      rowh += "<div class='mgr-funds'>" + funds.map(function (x) {
        var fw2 = Math.abs(x.v) / fmax * 100;
        return "<div class='rk subfund' data-k='" + x.f.fid + "' style='cursor:pointer'><div style='width:16px'></div>"
          + "<div class='rkbar' style='height:18px;background:var(--row-alt)'><i style='width:" + fw2 + "%;background:" + (x.v >= 0 ? "var(--pos)" : "var(--neg)") + ";opacity:.4'></i><b style='font-size:11.5px'>" + esc(x.f.fondo) + "</b></div>"
          + "<div class='rkv " + (x.v >= 0 ? "pos" : "neg") + "' style='font-size:11.5px'>" + fmtAum(x.v) + "</div></div>";
      }).join("") + "</div>";
    }
    return rowh;
  }).join("");
  html += "</div></section>";
  document.getElementById("view").innerHTML = html;
  document.querySelectorAll("#view .pill").forEach(function (b) { b.onclick = function () { S.flowCat = b.dataset.c; renderFlujos(); }; });
  var fw = document.getElementById("flowwin");
  if (fw) fw.querySelectorAll("button").forEach(function (b) { b.onclick = function () { S.flowWin = parseInt(b.dataset.w, 10); renderFlujos(); }; });
  document.querySelectorAll("#view .mgr-row").forEach(function (r) { r.onclick = function () { var nm = mgrs[+r.dataset.mi][0]; S.flowOpen[nm] = !S.flowOpen[nm]; renderFlujos(); }; });
  document.querySelectorAll("#view .subfund").forEach(function (r) { r.onclick = function (e) { e.stopPropagation(); openDetail(r.dataset.k); }; });
}
function monthLabels(n) {
  var names = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
  var base = M.fecha_base ? new Date(M.fecha_base) : new Date();
  var out = [];
  for (var i = n - 1; i >= 0; i--) { var d = new Date(base.getFullYear(), base.getMonth() - i, 1); out.push(names[d.getMonth()]); }
  return out;
}

// =====================================================================
//  FUND DETAIL MODAL
// =====================================================================
var detailState = { tab: "rend", k: null };
var detChart = null, paneAnimT = null;
function animateSheet(sheet, h0) {
  if (!sheet || !h0) return;
  var h1 = sheet.offsetHeight;
  if (Math.abs(h1 - h0) < 4) return;
  if (paneAnimT) clearTimeout(paneAnimT);
  sheet.style.height = h0 + "px";
  requestAnimationFrame(function () {
    sheet.style.transition = "height .24s ease";
    sheet.style.height = h1 + "px";
    paneAnimT = setTimeout(function () { sheet.style.transition = ""; sheet.style.height = ""; paneAnimT = null; }, 260);
  });
}
function openDetail(k) { detailState = { tab: "rend", k: k, rkPeriod: S.period, vcpRange: "max", vcpFrom: null, vcpTo: null, vcpMode: "valor" }; drawModal(); }
function closeModal() { if (detChart) { detChart.destroy(); detChart = null; } document.getElementById("modal").innerHTML = ""; }
function drawModal() {
  var f = FMAP[detailState.k]; if (!f) return;
  var fav = S.favs.has(detailState.k), inCmp = S.cmp.indexOf(detailState.k) >= 0;
  var badges = "<span class='tag'>" + esc(f.sub) + "</span><span class='tag'>" + esc(f.moneda) + "</span><span class='tag'>" + esc(f.settle) + "</span>"
    + (f.horizonte ? "<span class='tag'>" + esc(f.horizonte) + "</span>" : "")
    + (f.aum_real ? "<span class='tag real'>AUM real</span>" : "<span class='tag syn'>AUM s/d</span>");
  var tabs = [["rend", "Rendimientos"], ["vcp", "Cuotaparte"], ["flows", "Flujos"], ["ficha", "Ficha"]];
  document.getElementById("modal").innerHTML = "<div class='scrim' id='scrim'><div class='sheet' id='sheet'><div class='sheet-head'>"
    + "<div><h2>" + esc(f.fondo) + "</h2><div class='muted' style='font-size:12.5px'>" + esc(f.soc || "") + (f.n_clases > 1 ? " · " + f.n_clases + " clases" : "") + "</div><div class='badges'>" + badges + "</div></div>"
    + "<button class='x' id='mx'>×</button></div>"
    + "<div style='display:flex;gap:7px;padding:8px 16px 0;flex-wrap:wrap'>"
    + "<button class='btn-ghost' id='mfav'>" + (fav ? "★ En watchlist" : "☆ Seguir") + "</button>"
    + "<button class='btn-ghost' id='mcmp'>" + (inCmp ? "✓ En comparador" : "+ Comparar") + "</button>"
    + "<button class='btn-ghost' id='mexp'>⬇ Exportar imagen</button></div>"
    + "<div class='dtabs' id='dtabs'>" + tabs.map(function (t) { return "<button class='dtab" + (detailState.tab === t[0] ? " on" : "") + "' data-t='" + t[0] + "'>" + t[1] + "</button>"; }).join("") + "</div>"
    + "<div class='dpane' id='dpane'></div></div></div>";
  document.getElementById("scrim").onclick = function (e) { if (e.target.id === "scrim") closeModal(); };
  document.getElementById("mx").onclick = closeModal;
  document.getElementById("mfav").onclick = function () { toggleFav(detailState.k); this.textContent = S.favs.has(detailState.k) ? "★ En watchlist" : "☆ Seguir"; };
  document.getElementById("mcmp").onclick = function () {
    var k = detailState.k;
    if (S.cmp.indexOf(k) >= 0) S.cmp = S.cmp.filter(function (x) { return x !== k; }); else S.cmp.push(k);
    saveCmp(); renderTabs(); this.textContent = S.cmp.indexOf(k) >= 0 ? "✓ En comparador" : "+ Comparar";
  };
  document.getElementById("mexp").onclick = function () { exportSheet(f); };
  document.querySelectorAll("#dtabs .dtab").forEach(function (b) { b.onclick = function () { detailState.tab = b.dataset.t; drawPane(); }; });
  drawPane();
}
function drawPane() {
  var f = FMAP[detailState.k]; var pane = document.getElementById("dpane"); if (!pane) return;
  var sheet = document.getElementById("sheet"), h0 = sheet ? sheet.offsetHeight : 0;
  if (detChart) { detChart.destroy(); detChart = null; }
  document.querySelectorAll("#dtabs .dtab").forEach(function (b) { b.classList.toggle("on", b.dataset.t === detailState.tab); });
  var t = detailState.tab;
  if (t === "rend") {
    pane.innerHTML = "<div class='cards'>"
      + kpi("VCP", (f.vcp != null ? Number(f.vcp).toLocaleString("es-AR", { maximumFractionDigits: 2 }) : "—"))
      + kpi("AUM", fmtAum(f.aum), f.aum_real ? "real" : "s/d")
      + kpi("Costo admin.", fmtPct(f.fee_admin, 2), "anual")
      + kpi("Salida", f.fee_out != null ? fmtPct(f.fee_out, 2) : "—")
      + kpi("Mínimo", f.min ? fmtMoney(f.min) : "—")
      + kpi("Liquidación", f.settle) + "</div>"
      + posSection(f)
      + "<h4 style='margin:16px 0 6px;color:var(--accent);font-size:12px;text-transform:uppercase;letter-spacing:.4px'>Clase principal (" + esc((f.clases[0] || {}).clase || "") + ") · por moneda</h4>"
      + "<table><thead><tr><th style='text-align:left'>Período</th><th class='num'>TNA</th><th class='num'>Directo</th><th class='num'>USD @MEP</th><th class='num'>Real (CER)</th></tr></thead><tbody>"
      + PERIODS.map(function (p) {
        return "<tr><td style='text-align:left'>" + PL[p] + "</td>"
          + "<td class='num " + cls(getRet(f, p, "tna", "ars")) + "'>" + fmtPct(getRet(f, p, "tna", "ars")) + "</td>"
          + "<td class='num " + cls(getRet(f, p, "directo", "ars")) + "'>" + fmtPct(getRet(f, p, "directo", "ars")) + "</td>"
          + "<td class='num " + cls(getRet(f, p, "directo", "usd")) + "'>" + fmtPct(getRet(f, p, "directo", "usd")) + "</td>"
          + "<td class='num " + cls(getRet(f, p, "directo", "real")) + "'>" + fmtPct(getRet(f, p, "directo", "real")) + "</td></tr>";
      }).join("") + "</tbody></table>"
      + "<h4 style='margin:15px 0 6px;color:var(--accent);font-size:12px;text-transform:uppercase;letter-spacing:.4px'>Clases / cuotapartes (" + f.clases.length + ") · " + (S.metric === "tna" ? "TNA" : "Directo") + lensTag() + "</h4>"
      + "<div style='overflow:auto'><table><thead><tr><th style='text-align:left'>Clase</th><th>Mon</th>"
      + PERIODS.map(function (p) { return "<th class='num'>" + PL[p] + "</th>"; }).join("") + "<th class='num'>AUM</th><th class='num'>Costo</th><th class='num'>Mín.</th></tr></thead><tbody>"
      + f.clases.map(function (c) {
        return "<tr><td style='text-align:left'>" + esc(c.clase) + "</td><td class='num'>" + esc(c.moneda) + "</td>"
          + PERIODS.map(function (p) { var v = getRet(c, p, S.metric, S.lens); return "<td class='num " + cls(v) + "'>" + fmtPct(v) + "</td>"; }).join("")
          + "<td class='num'>" + fmtAum(c.aum) + (c.aum_real ? "" : " *") + "</td><td class='num'>" + fmtPct(c.fee_admin, 2) + "</td><td class='num'>" + (c.min ? fmtMoney(c.min) : "—") + "</td></tr>";
      }).join("") + "</tbody></table></div>";
    var pp = document.getElementById("posper");
    if (pp) pp.querySelectorAll("button").forEach(function (b) { b.onclick = function () { detailState.rkPeriod = b.dataset.p; drawPane(); }; });
  } else if (t === "vcp") {
    var w = vcpWindow(f);
    if (!w) { pane.innerHTML = "<p class='empty'>Sin histórico de cuotaparte para este fondo.</p>"; }
    else {
      var axis = M.hist_axis, isvar = detailState.vcpMode === "var";
      var ret = w.data.length > 1 ? (w.data[w.data.length - 1] / w.data[0] - 1) * 100 : null;
      var data = w.data.slice();
      if (isvar && data.length) { var b0 = data[0]; data = data.map(function (v) { return (v / b0 - 1) * 100; }); }
      var lensSel = "<span class='seg' id='vcplens'>"
        + "<button data-l='ars' class='" + (S.lens === "ars" ? "on" : "") + "'>" + (f.moneda === "USD" ? "USD orig." : "ARS orig.") + "</button>"
        + "<button data-l='usd' class='" + (S.lens === "usd" ? "on" : "") + "'>USD @MEP</button>"
        + "<button data-l='real' class='" + (S.lens === "real" ? "on" : "") + "'>Real (CER)</button></span>";
      var modeSel = "<span class='seg' id='vcpmode'><button data-m='valor' class='" + (!isvar ? "on" : "") + "'>Valor</button><button data-m='var' class='" + (isvar ? "on" : "") + "'>Variación %</button></span>";
      var ranges = [["7d", "7D"], ["1m", "1M"], ["3m", "3M"], ["6m", "6M"], ["ytd", "YTD"], ["max", "Máx"]];
      var rangeSel = "<span class='seg' id='vcprange'>" + ranges.map(function (r) { return "<button data-r='" + r[0] + "' class='" + (detailState.vcpRange === r[0] ? "on" : "") + "'>" + r[1] + "</button>"; }).join("") + "</span>";
      var dateInputs = "<span class='lbl'>Desde</span><input type='date' id='vcpfrom' min='" + axis[0] + "' max='" + axis[axis.length - 1] + "' value='" + w.from + "' style='padding:4px 6px;font-size:12px'>"
        + "<span class='lbl'>Hasta</span><input type='date' id='vcpto' min='" + axis[0] + "' max='" + axis[axis.length - 1] + "' value='" + w.to + "' style='padding:4px 6px;font-size:12px'>";
      pane.innerHTML =
        "<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 8px'><span class='lbl'>Moneda</span>" + lensSel + "<span class='lbl' style='margin-left:6px'>Ver</span>" + modeSel + "</div>"
        + "<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 9px'><span class='lbl'>Rango</span>" + rangeSel + dateInputs + "</div>"
        + "<div class='chartbox' style='height:235px'><canvas id='dc'></canvas></div>"
        + "<p class='note'>" + (isvar ? "Variación de la cuotaparte" : "Cuotaparte") + " en <b>" + (isvar ? "%" : w.unit) + "</b>"
        + (ret != null ? " · " + w.from + " → " + w.to + ": <b class='" + cls(ret) + "'>" + fmtPct(ret) + "</b>" : "")
        + (f.hist_real ? " · histórico real" : " · histórico reconstruido de los retornos reales") + ".</p>";
      if (data.length < 2) { document.getElementById("dc").parentNode.innerHTML = "<p class='empty'>Rango muy corto para mostrar.</p>"; }
      else detChart = new Chart(document.getElementById("dc"), { type: "line", data: { labels: w.labels, datasets: [{ label: (isvar ? "Var % " : "VCP ") + "(" + w.unit + ")", data: data, borderColor: COLORS[0], borderWidth: 2.4, pointRadius: 0, tension: .2, fill: true, backgroundColor: "rgba(58,95,207,.08)" }] }, options: chartOpts(isvar ? { pct: true } : { vcp: true }) });
      var vl = document.getElementById("vcplens");
      if (vl) vl.querySelectorAll("button").forEach(function (b) { b.onclick = function () { S.lens = b.dataset.l; drawPane(); }; });
      var vm = document.getElementById("vcpmode");
      if (vm) vm.querySelectorAll("button").forEach(function (b) { b.onclick = function () { detailState.vcpMode = b.dataset.m; drawPane(); }; });
      var vr = document.getElementById("vcprange");
      if (vr) vr.querySelectorAll("button").forEach(function (b) { b.onclick = function () { detailState.vcpRange = b.dataset.r; detailState.vcpFrom = vcpRangeFrom(b.dataset.r); detailState.vcpTo = axis[axis.length - 1]; drawPane(); }; });
      var vf = document.getElementById("vcpfrom");
      if (vf) vf.onchange = function () { detailState.vcpFrom = vf.value; detailState.vcpRange = "custom"; drawPane(); };
      var vt = document.getElementById("vcpto");
      if (vt) vt.onchange = function () { detailState.vcpTo = vt.value; detailState.vcpRange = "custom"; drawPane(); };
    }
  } else if (t === "flows") {
    if (!f.flows_real) {
      pane.innerHTML = "<p class='empty'>Flujos en acumulación para este fondo (Δcuotapartes × VCP). "
        + "El histórico se junta a diario; volvé en unas ruedas.</p>";
    } else {
      var months = monthLabels(12);
      var maxAbs = Math.max.apply(null, (f.flows || []).map(Math.abs).concat([1]));
      pane.innerHTML = "<div class='flowbars'>" + (f.flows || []).map(function (v, i) {
        var h = Math.abs(v) / maxAbs * 100;
        return "<div class='fb'><div class='b " + (v >= 0 ? "pos" : "neg") + "' style='height:" + h + "%' title='" + months[i] + ": " + fmtAum(v) + "'></div><small>" + months[i] + "</small></div>";
      }).join("") + "</div><p class='note'>Flujo neto mensual (suscripciones − rescates) = Δcuotapartes × VCP.</p>";
    }
  } else if (t === "ficha") {
    pane.innerHTML = "<dl class='ficha'>"
      + fila("Gestora", f.soc) + fila("Depositaria", f.dep) + fila("Categoría", f.tipo) + fila("Subcategoría", f.sub)
      + fila("Moneda", f.moneda_full) + fila("Horizonte", f.horizonte) + fila("Duration", f.duration)
      + fila("Liquidación rescate", f.settle) + fila("Inversión mínima", f.min ? fmtMoney(f.min) : null)
      + fila("Costo gestión (anual)", fmtPct(f.fee_admin, 2)) + fila("Comisión salida", f.fee_out != null ? fmtPct(f.fee_out, 2) : null)
      + fila("Inicio", f.inicio) + fila("ISIN", f.isin) + fila("Bloomberg", f.bbg)
      + “</dl>” + (f.obj ? “<p class='objetivo'>”” + esc(f.obj) + (f.obj.length >= 400 ? “…” : “”) + “”</p>” : “”);
  }
  pane.style.opacity = "0"; requestAnimationFrame(function () { if (pane) pane.style.opacity = "1"; });
  animateSheet(sheet, h0);
}
function kpi(k, v, sub) { return "<div class='kpi'><div class='k'>" + k + "</div><div class='v'>" + v + (sub ? " <small>" + sub + "</small>" : "") + "</div></div>"; }
function fila(k, v) { return v ? ("<dt>" + k + "</dt><dd>" + esc(String(v)) + "</dd>") : ""; }

// posición del fondo en su subcategoría (percentil)
function fundPosition(f, period) {
  var peers = FUNDS.filter(function (x) { return x.sub === f.sub && getRet(x, period, S.metric, S.lens) != null; });
  var v = getRet(f, period, S.metric, S.lens);
  if (v == null || peers.length < 2) return null;
  var vals = peers.map(function (x) { return getRet(x, period, S.metric, S.lens); });
  var rank = 1; vals.forEach(function (x) { if (x > v) rank++; });
  var n = peers.length;
  return { v: v, rank: rank, n: n, pct: (n - rank) / (n - 1), med: median(vals), max: Math.max.apply(null, vals), min: Math.min.apply(null, vals) };
}
function posSection(f) {
  var per = detailState.rkPeriod || S.period;
  var pers = "<span class='seg' id='posper'>" + PERIODS.map(function (p) { return "<button data-p='" + p + "' class='" + (per === p ? "on" : "") + "'>" + PL[p] + "</button>"; }).join("") + "</span>";
  var head = "<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:2px 0 7px'><span class='lbl'>Posición en " + esc(f.sub) + "</span>" + pers + "</div>";
  var pos = fundPosition(f, per);
  if (!pos) return head + "<p class='note'>Pocos pares en la subcategoría para rankear.</p>";
  var col = pos.v >= pos.med ? "var(--pos)" : "var(--neg)";
  return head
    + "<div class='posbar'><div class='medm' style='left:50%'></div><div class='mk' style='left:" + pos.pct * 100 + "%;background:" + col + "' title='este fondo'></div></div>"
    + "<div class='posends'><span>peor</span><span>mediana</span><span>mejor</span></div>"
    + "<div class='posstats'><b style='color:" + col + "'>#" + pos.rank + " de " + pos.n + "</b> · top " + Math.round(pos.rank / pos.n * 100) + "% · "
    + (S.metric === "tna" ? "TNA" : "Directo") + " " + PL[per] + lensTag() + ": <b>" + fmtPct(pos.v) + "</b> · mediana " + fmtPct(pos.med) + " · rango " + fmtPct(pos.min) + "…" + fmtPct(pos.max) + "</div>";
}

// ---------------- export ----------------
function exportNode(node, name) {
  if (!node) return;
  if (!window.html2canvas) { alert("html2canvas no cargó (sin internet?)"); return; }
  var bg = getComputedStyle(document.body).getPropertyValue("--page-bg") || "#fff";
  html2canvas(node, { backgroundColor: bg, scale: 2 }).then(function (canvas) {
    var a = document.createElement("a");
    a.download = (name || "fci").replace(/[^a-z0-9_]+/gi, "_") + ".png";
    a.href = canvas.toDataURL("image/png"); a.click();
  });
}
function exportSheet(f) { exportNode(document.getElementById("sheet"), "fci_" + (f.fondo || "fondo")); }

// ---------------- chart options ----------------
function compactNum(v) { var a = Math.abs(v); if (a >= 1e6) return (v / 1e6).toFixed(1) + "M"; if (a >= 1e3) return (v / 1e3).toFixed(1) + "k"; return a < 10 ? v.toFixed(2) : Math.round(v).toString(); }
function chartOpts(o) {
  o = o || {};
  var grid = cssvar("--panel-border"), txt = cssvar("--text-dim");
  function ax(v) { return o.money ? "$" + (v / 1000).toFixed(0) + "k" : (o.vcp ? compactNum(v) : v + "%"); }
  function tip(v) { return o.money ? "$" + Math.round(v).toLocaleString("es-AR") : (o.vcp ? Number(v).toLocaleString("es-AR", { maximumFractionDigits: 2 }) : v.toFixed(2) + "%"); }
  return { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
    plugins: { legend: { display: !!o.money, labels: { color: txt, boxWidth: 12, font: { size: 11 } } },
      tooltip: { callbacks: { label: function (c) { var v = c.parsed.y; return (c.dataset.label ? c.dataset.label + ": " : "") + tip(v); } } } },
    scales: { x: { ticks: { color: txt, maxTicksLimit: 8, font: { size: 10 } }, grid: { display: false } },
      y: { ticks: { color: txt, font: { size: 10 }, callback: ax }, grid: { color: grid } } } };
}
function cssvar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim() || "#888"; }

// ---------------- router ----------------
function render() {
  if (cmpChart) { cmpChart.destroy(); cmpChart = null; }
  renderTabs(); renderStrip();
  var srcs = (M.sources || {});
  document.getElementById("srcnote").innerHTML =
    "Fuentes — catálogo+retornos+VCP: <b>CAFCI</b> · AUM: <b>ArgentinaDatos</b> (" + M.n_aum_real + "/" + M.n_shown + ") · "
    + "lente USD/CER: <b>BCRA</b> (A3500/CER) · histórico: real (fci_history) o reconstruido de retornos reales · "
    + "flujos: <b>" + (M.flows_real ? "reales (Δccp×VCP)" : "acumulando") + "</b>. Composición no disponible (sin fuente pública). "
    + "Subcategorías derivadas de campos CAFCI.";
  ({ mercado: renderMercado, rankings: renderRankings, comparar: renderComparar, favoritos: renderFavoritos, flujos: renderFlujos }[S.view] || renderMercado)();
}
window.App = { rerenderCharts: function () { if (detailState.k && document.getElementById("dpane")) drawPane(); render(); } };
document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeModal(); });
// recolorear charts al cambiar el tema (toggle ◑ de base.html cambia data-theme)
new MutationObserver(function () { if (window.App && M) window.App.rerenderCharts(); })
  .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

// ---------------- bootstrap ----------------
function boot() {
  M = D.meta; FUNDS = D.funds || [];
  PERIODS = M.periods; PL = M.period_labels;
  PDAYS = { dias_7: 7, mes_1: 30, dias_90: 90, dias_180: 180, ytd: 155, meses_12: 365 };
  PDAYS.ytd = (function () { if (!M.fecha_base) return 155; var d = new Date(M.fecha_base); return Math.max(1, Math.round((d - new Date(d.getFullYear(), 0, 0)) / 864e5)); })();
  FMAP = {}; FUNDS.forEach(function (f) { FMAP[key(f)] = f; });
  S.favs = new Set(Array.from(S.favs).filter(function (k) { return FMAP[k]; })); saveFavs();
  S.cmp = S.cmp.filter(function (k) { return FMAP[k]; }); saveCmp();
  render();
}
(function load() {
  var view = document.getElementById("view");
  if (view) view.innerHTML = "<p class='empty'>Cargando FCI…</p>";
  fetch("/fci/data").then(function (r) { return r.json(); }).then(function (d) { D = d; boot(); })
    .catch(function () { if (view) view.innerHTML = "<p class='empty'>No se pudo cargar /fci/data.</p>"; });
})();
})();
