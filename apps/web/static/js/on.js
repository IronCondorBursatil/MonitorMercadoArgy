/* AUTO-GENERADO por scripts/build_on_static.py — NO editar a mano.
   App cliente de /on (porteada de apps/web/on_src/on_app.html).
   Datos en vivo desde /on/data; tema y chrome los da base.html. */

/* ---- sectores (window.ON_SECTORS / ON_SECTOR_MAP) ---- */
/* Sectores ON: orden canónico + paleta + íconos. Compartido por los 20 mockups.
   El campo `sector` ya viene calculado por bono en on_data.js (build_on_mockup_data.py).
   Acá solo definimos color/orden/etiqueta-corta para pintar consistente. */
window.ON_SECTORS = [
  { key: "Energía / Petróleo & Gas",        short: "Energía",    color: "#ED8B36", icon: "⚡" },
  { key: "Utilities (Luz / Gas)",            short: "Utilities",  color: "#2FA4C9", icon: "🔌" },
  { key: "Servicios Financieros",            short: "Serv. Financieros", color: "#6C5CE0", icon: "🏦" },
  { key: "Agro / Alimentos",                 short: "Agro",       color: "#2FB36B", icon: "🌾" },
  { key: "Industrial / Maquinaria",          short: "Industrial", color: "#C0573C", icon: "⚙️" },
  { key: "Infraestructura / Construcción",   short: "Infra",      color: "#C9A227", icon: "🏗️" },
  { key: "Real Estate",                      short: "Real Estate",color: "#D85FA0", icon: "🏢" },
  { key: "Telecomunicaciones",               short: "Telco",      color: "#0E9C8A", icon: "📡" },
  { key: "Salud / Farma",                     short: "Salud",      color: "#E0566E", icon: "💊" },
  { key: "Minería",                          short: "Minería",    color: "#8D6E63", icon: "⛏️" },
  { key: "Otros",                            short: "Otros",      color: "#8993B8", icon: "•" },
];
window.ON_SECTOR_MAP = Object.fromEntries(window.ON_SECTORS.map(s => [s.key, s]));

/* ---- librería ON (de on_src/util.js, sin manejo de tema/header) ---- */
/* ON mockups — librería compartida. Namespace global `ON`.
   Datos reales en window.ON_DATA (on_data.js); sectores en window.ON_SECTORS (sectors.js).
   Diseñada para que los 20 mockups consuman la MISMA API y se vean consistentes. */
(function () {
  "use strict";
  var D = window.ON_DATA || { bonds: [], sectors: [], meta: {} };
  var SECTORS = window.ON_SECTORS || [];
  var SMAP = window.ON_SECTOR_MAP || {};

  // ---- acceso a datos -------------------------------------------------------
  function legs(ccy) {                       // bonos de una moneda (default MEP)
    ccy = ccy || "MEP";
    return D.bonds.filter(function (b) { return b.ccy === ccy; });
  }
  function byLey(list) {                      // {AR:[...], EXT:[...]}
    var o = { AR: [], EXT: [] };
    list.forEach(function (b) { (o[b.ley] || (o[b.ley] = [])).push(b); });
    return o;
  }
  function bySector(list) {                   // Map sector(orden canónico) -> [bonos]
    var m = new Map();
    SECTORS.forEach(function (s) { m.set(s.key, []); });
    list.forEach(function (b) {
      if (!m.has(b.sector)) m.set(b.sector, []);
      m.get(b.sector).push(b);
    });
    // descartar sectores vacíos preservando orden
    var out = new Map();
    m.forEach(function (v, k) { if (v.length) out.set(k, v); });
    return out;
  }
  function sectorColor(key) { return (SMAP[key] && SMAP[key].color) || "#8993B8"; }
  function sectorMeta(key) { return SMAP[key] || { key: key, short: key, color: "#8993B8", icon: "•" }; }

  // ---- formato --------------------------------------------------------------
  function pct(v, dec) { if (v == null || isNaN(v)) return "—"; return v.toFixed(dec == null ? 2 : dec) + "%"; }
  function pctSigned(v, dec) { if (v == null || isNaN(v)) return "—"; return (v >= 0 ? "+" : "") + v.toFixed(dec == null ? 2 : dec) + "%"; }
  function num(v, dec) { if (v == null || isNaN(v)) return "—"; return v.toLocaleString("es-AR", { minimumFractionDigits: dec == null ? 2 : dec, maximumFractionDigits: dec == null ? 2 : dec }); }
  function vol(v) {
    if (v == null || isNaN(v)) return "—";
    if (v >= 1e9) return (v / 1e9).toFixed(1) + "B";
    if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return String(Math.round(v));
  }
  function date(iso) {                        // "2026-06-30" -> "30/06/26"
    if (!iso) return "—";
    var p = iso.split("-");
    return p[2] + "/" + p[1] + "/" + p[0].slice(2);
  }
  function sign(v) { return v == null || isNaN(v) ? "" : (v >= 0 ? "pos" : "neg"); }

  // ---- escape HTML (anti-XSS) ----------------------------------------------
  // Usado para inyectar strings editables del ABM (emisor, clase, sector.short)
  // en innerHTML/title. Escapa los 5 caracteres peligrosos en HTML/atributos.
  function esc(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // ---- estadística ----------------------------------------------------------
  function avg(arr) { var a = arr.filter(function (x) { return x != null && !isNaN(x); }); return a.length ? a.reduce(function (s, x) { return s + x; }, 0) / a.length : null; }
  function median(arr) { var a = arr.filter(function (x) { return x != null && !isNaN(x); }).sort(function (x, y) { return x - y; }); if (!a.length) return null; var m = Math.floor(a.length / 2); return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2; }
  function sum(arr) { return arr.reduce(function (s, x) { return s + (x || 0); }, 0); }

  // ---- ajuste log: TIR = a + b·ln(MD) --------------------------------------
  function logFit(points) {                   // points: [{x,y}] con x=MD>0
    var pts = points.filter(function (p) { return p.x > 0 && p.y != null && !isNaN(p.y); });
    if (pts.length < 3) return null;
    var n = pts.length, sx = 0, sy = 0, sxx = 0, sxy = 0;
    pts.forEach(function (p) { var lx = Math.log(p.x); sx += lx; sy += p.y; sxx += lx * lx; sxy += lx * p.y; });
    var b = (n * sxy - sx * sy) / (n * sxx - sx * sx);
    var a = (sy - b * sx) / n;
    return {
      a: a, b: b,
      predict: function (x) { return a + b * Math.log(x); },
      curve: function (xmin, xmax, steps) {
        steps = steps || 40; var out = [];
        for (var i = 0; i <= steps; i++) { var x = xmin + (xmax - xmin) * i / steps; if (x <= 0) continue; out.push({ x: x, y: a + b * Math.log(x) }); }
        return out;
      },
    };
  }

  // ---- helpers de chart -----------------------------------------------------
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function chartTheme() {
    return {
      accent: cssVar("--accent") || "#3a5fcf",
      txt: cssVar("--text-dim") || "#4a5780",
      grid: cssVar("--panel-border") || "#d2d8e6",
      panel: cssVar("--panel-bg") || "#fff",
      pos: cssVar("--pos") || "#0d6e3a",
      neg: cssVar("--neg") || "#a8242b",
    };
  }
  // Opciones base para un scatter TIR(y) vs MD(x). Extender/override según el mockup.
  function baseScatterOpts(o) {
    o = o || {};
    var t = chartTheme();
    return {
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { title: { display: true, text: o.xLabel || "Modified Duration (años)", color: t.txt }, ticks: { color: t.txt }, grid: { color: t.grid } },
        y: { title: { display: true, text: o.yLabel || "TIR (%)", color: t.txt }, ticks: { color: t.txt }, grid: { color: t.grid } },
      },
      plugins: {
        legend: { labels: { color: t.txt, usePointStyle: true, boxWidth: 8 } },
        tooltip: { callbacks: { label: function (c) { var r = c.raw || {}; return (r.t ? r.t + " · " : "") + (r.e || "") + "  MD " + (r.x != null ? r.x.toFixed(2) : "?") + " · TIR " + (r.y != null ? r.y.toFixed(2) : "?") + "%"; } } },
      },
    };
  }
  // Convierte una lista de bonos a puntos de scatter TIR/MD (descarta sin MD/TIR).
  function scatterPoints(list) {
    return list.filter(function (b) { return b.md != null && b.md > 0 && b.tir != null; })
      .map(function (b) { return { x: b.md, y: b.tir, t: b.ticker, e: b.emisor, sector: b.sector, ley: b.ley, vol: b.volume, paridad: b.paridad }; });
  }

  // ---- tema (light/dark) ----------------------------------------------------
  function initTheme() { var t = localStorage.getItem("on_theme") || localStorage.getItem("theme"); if (t) document.documentElement.setAttribute("data-theme", t); }
  function toggleTheme() {
    var h = document.documentElement;
    var t = h.getAttribute("data-theme") === "dark" ? "light" : "dark";
    h.setAttribute("data-theme", t); localStorage.setItem("on_theme", t);
    window.dispatchEvent(new Event("on:themechange"));
  }

  // Hidrata SECTORS/SMAP desde el payload del server (ON.DATA.sectors_meta), que es
  // el espejo de la fuente Python on_classification.SECTORS — así cliente y SSR pintan
  // el mismo sector/color sin re-sincronizar a mano la copia horneada (sectors.js).
  // MUTA in-place (no reasigna): util captura SECTORS/SMAP por referencia en el init y
  // los exporta como ON.SECTORS; reasignar dejaría stale el closure. Si meta viene vacío
  // (offline / payload viejo) NO toca nada → conserva la copia horneada como fallback.
  function syncSectors(meta) {
    if (!meta || !meta.length) return;
    SECTORS.splice(0, SECTORS.length);
    Object.keys(SMAP).forEach(function (k) { delete SMAP[k]; });
    meta.forEach(function (s) { SECTORS.push(s); SMAP[s.key] = s; });
    window.ON_SECTORS = SECTORS; window.ON_SECTOR_MAP = SMAP;
  }

  window.ON = {
    DATA: D, SECTORS: SECTORS,
    legs: legs, byLey: byLey, bySector: bySector,
    sectorColor: sectorColor, sectorMeta: sectorMeta,
    esc: esc,
    pct: pct, pctSigned: pctSigned, num: num, vol: vol, date: date, sign: sign,
    avg: avg, median: median, sum: sum,
    logFit: logFit, cssVar: cssVar, chartTheme: chartTheme,
    baseScatterOpts: baseScatterOpts, scatterPoints: scatterPoints,
    initTheme: initTheme, toggleTheme: toggleTheme, syncSectors: syncSectors,
  };
})();

/* ---- herramienta unificada Sector›Emisor›Título (de on_src/unified.js) ---- */
/* ============================================================
   Herramienta unificada ON — Sector › Emisor › Título en UNA tabla con
   agregados por grupo. Reemplaza "Liga de sectores" (agregado por sector) +
   "Heatmap de emisores" (agregado por emisor) + drill-down (por título):
   los tres niveles, colapsables, en una sola herramienta.

   Consume window.ON / ON.DATA (util.js debe cargar antes). Namespace global Unified.

   API:
     Unified.render(mountEl, { onRender, defaultCcy })   // monta toolbar + tabla
     Unified.visibleBonds()      -> bonos visibles (post-filtro, moneda activa)
     Unified.sectorAggs()        -> [{key,label,short,color,icon,count,tir_avg,md_avg,n_ar,n_ext,vol}]
     Unified.highlight(key)      -> resalta un sector (atenúa el resto); null = limpia
     Unified.on(evt, cb)         -> 'render'(={bonds,aggs}) | 'sector'(key) | 'bond'(tk) | 'hover'(tk)
   `onRender(bonds, aggs)` se llama tras cada (re)render → el host sincroniza su gráfico.
   ============================================================ */
(function () {
  "use strict";
  var ON = window.ON;

  // columnas de datos (el ticker va en la 1ª columna de grupo/etiqueta)
  // De Clase en adelante todo centrado. `num` = fuente monoespaciada (tabular).
  var COLS = [
    { k: "clase",      label: "Clase",      align: "center" },
    { k: "isin",       label: "ISIN",       align: "center", num: true },
    { k: "ley",        label: "Ley",        align: "center" },
    { k: "tipo",       label: "Tipo",       align: "center" },
    { k: "emision",    label: "Emisión",    align: "center", num: true },
    { k: "vto",        label: "Vto",        align: "center", num: true },
    { k: "cupon",      label: "Cupón",      align: "center", num: true },
    { k: "frec",       label: "Frec.",      align: "center" },
    { k: "dias_cupon", label: "Días cupón", align: "center", num: true },
    { k: "price",      label: "Precio",     align: "center", num: true },
    { k: "paridad",    label: "Paridad",    align: "center", num: true },
    { k: "tir",        label: "TIR",        align: "center", num: true },
    { k: "cy",         label: "CY",         align: "center", num: true },
    { k: "md",         label: "MD",         align: "center", num: true },
    { k: "convex",     label: "Convex.",    align: "center", num: true },
    { k: "change_pct", label: "%Día",       align: "center", num: true },
    { k: "volume",     label: "Vol",        align: "center", num: true },
  ];
  var FREC = { 1: "Anual", 2: "Semestral", 3: "Cuatrim.", 4: "Trimestral", 6: "Bimestral", 12: "Mensual" };

  var state = {
    ccy: "MEP", q: "", tipo: { HD: true, DL: true },
    sortKey: "tir", sortDir: -1,
    colSec: new Set(), colEmi: new Set(),
    highlight: null,
  };
  var mountEl = null, hostOpts = {}, listeners = {};

  function emit(evt, p) { (listeners[evt] || []).forEach(function (cb) { cb(p); }); }

  // ---- agregados / agrupación ----
  function agg(bonds) {
    return {
      count: bonds.length,
      tir_avg: ON.avg(bonds.map(function (b) { return b.tir; })),
      md_avg: ON.avg(bonds.map(function (b) { return b.md; })),
      n_ar: bonds.filter(function (b) { return b.ley === "AR"; }).length,
      n_ext: bonds.filter(function (b) { return b.ley === "EXT"; }).length,
      vol: ON.sum(bonds.map(function (b) { return b.volume || 0; })),
    };
  }

  function visible() {
    var q = state.q.trim().toUpperCase();
    // Si el host provee bondsFn (ej. el sidebar de facetas global de /on), usamos ESA
    // lista ya filtrada y no aplicamos el ccy/tipo interno (los maneja el host). Si no,
    // filtramos internamente por moneda + tipo (modo standalone de los mockups).
    var src = hostOpts.bondsFn
      ? (hostOpts.bondsFn() || [])
      : ON.DATA.bonds.filter(function (b) {
          return b.ccy === state.ccy && !(b.tipo && !state.tipo[b.tipo]);
        });
    if (!q) return src;
    return src.filter(function (b) {
      return (b.ticker + " " + (b.emisor || "") + " " + (b.clase || "") + " " + (b.isin || "")).toUpperCase().indexOf(q) >= 0;
    });
  }

  function tree(bonds) {                       // sector -> emisor -> [bonos]
    var m = new Map();
    bonds.forEach(function (b) {
      var sk = b.sector || "Otros", ek = b.emisor || "—";
      if (!m.has(sk)) m.set(sk, new Map());
      var em = m.get(sk);
      if (!em.has(ek)) em.set(ek, []);
      em.get(ek).push(b);
    });
    return m;
  }

  function sectorAggs() {
    var t = tree(visible()), out = [];
    function push(sk) {
      var all = []; t.get(sk).forEach(function (arr) { all = all.concat(arr); });
      var m = ON.sectorMeta(sk), a = agg(all);
      a.key = sk; a.label = sk; a.short = m.short; a.color = m.color; a.icon = m.icon;
      out.push(a);
    }
    ON.SECTORS.forEach(function (s) { if (t.has(s.key)) push(s.key); });
    t.forEach(function (_v, sk) { if (!out.some(function (o) { return o.key === sk; })) push(sk); });
    out.sort(function (a, b) { return (b.tir_avg || 0) - (a.tir_avg || 0); });  // liga: TIR prom desc
    return out;
  }

  function sortBonds(bonds) {
    var k = state.sortKey, d = state.sortDir;
    return bonds.slice().sort(function (a, b) {
      var x = a[k], y = b[k];
      if (x == null && y == null) return 0;
      if (x == null) return 1; if (y == null) return -1;
      if (typeof x === "string") return d * x.localeCompare(y);
      return d * (x - y);
    });
  }

  // ---- formato de celdas ----
  function cell(k, b) {
    var v = b[k];
    if (k === "clase") return b.clase ? '<span class="uni-cl">' + ON.esc(b.clase) + '</span>' : '<span class="dim">—</span>';
    if (k === "isin") return b.isin ? '<span class="uni-isin">' + ON.esc(b.isin) + '</span>' : '<span class="dim">—</span>';
    if (k === "ley") return '<span class="uni-ley ' + b.ley + '">' + b.ley + '</span>';
    if (k === "tipo") return b.tipo ? '<span class="uni-tipo ' + b.tipo + '">' + b.tipo + '</span>' : '—';
    if (k === "emision") return ON.date(b.emision);
    if (k === "vto") return ON.date(b.vto);
    if (k === "cupon") return v != null ? ON.pct(v) : '<span class="dim">—</span>';
    if (k === "frec") return v != null ? (FREC[v] || (v + "×/año")) : "—";
    if (k === "dias_cupon") return v != null ? v : "—";
    if (k === "price") return ON.num(v);
    if (k === "paridad") return ON.pct(v, 1);
    if (k === "tir") return ON.pct(v);
    if (k === "cy") return ON.pct(v);
    if (k === "md") return v != null ? ON.num(v) : "—";
    if (k === "convex") return v != null ? ON.num(v) : "—";
    if (k === "change_pct") return '<span class="' + ON.sign(v) + '">' + ON.pctSigned(v) + '</span>';
    if (k === "volume") return ON.vol(v);
    return v == null ? "—" : v;
  }
  function bar(v, max, color) {
    var w = (max > 0 && v != null) ? Math.max(3, Math.min(100, v / max * 100)) : 0;
    return '<span class="uni-barwrap"><span class="uni-bar" style="width:' + w + '%;background:' + color + '"></span></span>';
  }

  // ---- badge de cambio de calificación (monitor diario FIX SCR) ----
  // El dataset trae `rating_chg = {dir,from,to,fecha,persp_from,persp_to}` SÓLO mientras
  // el cambio está fresco (ventana de 7 días del server), así que acá no hay que filtrar
  // por fecha: si vino, se pinta. `dir` se whitelistea antes de entrar en un nombre de
  // clase y el texto del tooltip pasa por ON.esc (va a un atributo title).
  var CHG_GLYPH = { up: "▲", down: "▼", watch: "⚑" };
  function chgFecha(iso) {                     // "2026-08-28" → "28/08/2026" (año entero:
    var p = String(iso || "").split("-");      // el tooltip no compite por ancho con la grilla)
    return p.length === 3 ? p[2] + "/" + p[1] + "/" + p[0] : "";
  }
  function chgBadge(c) {
    if (!c || !c.dir) return "";
    var dir = c.dir === "up" ? "up" : (c.dir === "down" ? "down" : "watch");
    var when = chgFecha(c.fecha);
    // Un `watch` puede ser cambio de sola perspectiva (mismo rating): ahí el "X ← Y" de
    // ratings no diría nada, se describe la perspectiva.
    var txt = (c.to && c.from && c.to !== c.from)
      ? c.to + " ← " + c.from
      : "Perspectiva " + (c.persp_to || "—") + " ← " + (c.persp_from || "—");
    return '<span class="uni-chg uni-chg-' + dir + '" title="' +
      ON.esc(txt + (when ? " · " + when : "")) + '">' + CHG_GLYPH[dir] + '</span>';
  }
  // celdas de agregado alineadas a COLS (centradas). `show` = qué métricas mostrar:
  // sector = {tir, md, vol, bar}; emisor = {vol} (sin promedio de TIR ni MD — sólo rótulo).
  function aggCells(a, max, color, show) {
    var out = "";
    COLS.forEach(function (c) {
      if (show.tir && c.k === "tir") out += '<td class="num tir-cell" style="text-align:center">' + (show.bar ? bar(a.tir_avg, max, color) : "") + '<b>' + ON.pct(a.tir_avg) + '</b></td>';
      else if (show.md && c.k === "md") out += '<td class="num" style="text-align:center">' + (a.md_avg != null ? ON.num(a.md_avg) + '<span class="faint">a</span>' : "—") + '</td>';
      else if (show.vol && c.k === "volume") out += '<td class="num dim" style="text-align:center">' + ON.vol(a.vol) + '</td>';
      else out += "<td></td>";
    });
    return out;
  }

  // ---- render ----
  function render() {
    if (!mountEl) return;
    var bonds = visible(), t = tree(bonds), aggs = sectorAggs();
    var max = Math.max.apply(null, aggs.map(function (a) { return a.tir_avg || 0; }).concat([1]));

    var h = '<table class="uni"><thead><tr><th class="uni-grp-h uni-sort" data-k="ticker">Sector › Emisor › Título</th>';
    COLS.forEach(function (c) {
      var srt = c.k === state.sortKey ? (state.sortDir > 0 ? " sorted-asc" : " sorted-desc") : "";
      h += '<th data-k="' + c.k + '" class="uni-sort' + srt + '" style="text-align:' + c.align + '">' + c.label + '</th>';
    });
    h += '</tr></thead><tbody>';

    aggs.forEach(function (a) {
      var col = a.color, cS = state.colSec.has(a.key);
      var dim = state.highlight && state.highlight !== a.key;
      var label = '<span class="uni-caret">' + (cS ? "▶" : "▼") + '</span><span class="uni-ic">' + a.icon + '</span>' +
        '<b style="color:' + col + '">' + a.short + '</b><span class="uni-n">' + a.count + '</span>' +
        '<span class="uni-leysplit">' + (a.n_ar ? '<span class="ar">AR ' + a.n_ar + '</span>' : '') +
        (a.n_ext ? '<span class="ext">EXT ' + a.n_ext + '</span>' : '') + '</span>';
      var tr = '<tr class="uni-sector' + (cS ? "" : " open") + (dim ? " dimmed" : "") + '" data-sec="' + encodeURIComponent(a.key) + '" style="--sc:' + col + '">';
      if (cS) {
        // colapsado: etiqueta a la izquierda + agregados (vista overview)
        h += tr + '<td class="uni-grp">' + label + '</td>' +
          aggCells(a, max, col, { tir: true, md: true, vol: true, bar: true }) + '</tr>';
      } else {
        // desplegado: banner a TODO el ancho con el nombre centrado en la mitad del listado
        h += tr + '<td class="uni-grp uni-grp-banner" colspan="' + (COLS.length + 1) + '">' + label + '</td></tr>';
      }
      if (cS) return;

      var emis = Array.from(t.get(a.key).entries()).map(function (e) { return { name: e[0], bonds: e[1], a: agg(e[1]) }; });
      emis.sort(function (x, y) { return (y.a.tir_avg || 0) - (x.a.tir_avg || 0); });
      emis.forEach(function (e) {
        var ek = a.key + "||" + e.name, cE = state.colEmi.has(ek);
        // Emisor = banner a TODO el ancho (nombre entero) + calificación FIX SCR del emisor.
        var _rt = e.bonds[0] || {};
        var _cal = _rt.rating
          ? '<span class="uni-rating uni-rg-' + (_rt.rating_grade || "good") + '" title="Calificación largo plazo ' +
              ON.esc(_rt.rating) + (_rt.rating_persp ? ' · ' + ON.esc(_rt.rating_persp) : '') + '">' + ON.esc(_rt.rating) +
              (_rt.rating_persp && _rt.rating_persp !== "N/A" ? '<span class="uni-rp">' + ON.esc(_rt.rating_persp) + '</span>' : '') +
            '</span>'
          : '';
        // ▲/▼/⚑ pegado al rating: el cambio se lee en la MISMA fila del emisor, sin abrir nada.
        var _chg = chgBadge(_rt.rating_chg);
        h += '<tr class="uni-emisor" data-emi="' + encodeURIComponent(ek) + '" style="--sc:' + col + '">' +
          '<td class="uni-grp uni-grp2" colspan="' + (COLS.length + 1) + '"><span class="uni-caret">' + (cE ? "▶" : "▼") + '</span>' +
          '<span class="uni-em" title="' + ON.esc(e.name) + '">' + ON.esc(e.name) + '</span><span class="uni-n">' + e.a.count + '</span>' + _cal + _chg + '</td></tr>';
        if (cE) return;
        sortBonds(e.bonds).forEach(function (b) {
          h += '<tr class="uni-bond" data-tk="' + b.ticker + '" style="--sc:' + col + '">' +
            '<td class="uni-grp uni-grp3"><span class="uni-tk">' + ON.esc(b.ticker) + '</span></td>';
          COLS.forEach(function (c) {
            h += '<td class="' + (c.num ? "num" : "") + (c.k === "tir" ? " tir-cell" : "") +
              '" style="text-align:' + c.align + '">' + cell(c.k, b) + '</td>';
          });
          h += '</tr>';
        });
      });
    });
    h += '</tbody></table>';
    mountEl.innerHTML = h;
    wire();
    emit("render", { bonds: bonds, aggs: aggs });
    if (hostOpts.onRender) hostOpts.onRender(bonds, aggs);
  }

  function wire() {
    mountEl.querySelectorAll("th.uni-sort").forEach(function (th) {
      th.onclick = function () {
        var k = th.dataset.k;
        if (state.sortKey === k) state.sortDir = -state.sortDir;
        else { state.sortKey = k; state.sortDir = (k === "ticker" || k === "clase" || k === "isin") ? 1 : -1; }
        render();
      };
    });
    mountEl.querySelectorAll("tr.uni-sector").forEach(function (tr) {
      tr.onclick = function () {
        var k = decodeURIComponent(tr.dataset.sec);
        if (state.colSec.has(k)) state.colSec.delete(k); else state.colSec.add(k);
        render(); emit("sector", k);
      };
    });
    mountEl.querySelectorAll("tr.uni-emisor").forEach(function (tr) {
      tr.onclick = function (ev) {
        ev.stopPropagation();
        var k = decodeURIComponent(tr.dataset.emi);
        if (state.colEmi.has(k)) state.colEmi.delete(k); else state.colEmi.add(k);
        render();
      };
    });
    mountEl.querySelectorAll("tr.uni-bond").forEach(function (tr) {
      tr.onclick = function () { emit("bond", tr.dataset.tk); };
      tr.onmouseenter = function () { emit("hover", tr.dataset.tk); };
    });
  }

  function toolbar() {
    var el = document.createElement("div");
    el.className = "uni-toolbar";
    var h = '<input class="uni-search" type="search" placeholder="Buscar ticker / emisor / clase…">';
    if (!hostOpts.bondsFn) {                  // standalone: el tool maneja moneda + tipo
      h += '<div class="uni-seg" data-grp="ccy">' + ["ARS", "MEP", "CABLE"].map(function (c) {
        return '<button data-v="' + c + '"' + (c === state.ccy ? ' class="on"' : '') + '>' + c + '</button>';
      }).join("") + '</div>';
      h += '<div class="uni-seg" data-grp="tipo">' + ["HD", "DL"].map(function (c) {
        return '<button data-v="' + c + '" class="' + (state.tipo[c] ? "on" : "") + '">' + c + '</button>';
      }).join("") + '</div>';
    }
    h += '<button class="uni-btn" data-act="expand">Expandir</button>' +
      '<button class="uni-btn" data-act="collapse">Colapsar sectores</button>';
    var actions = hostOpts.actions || [];
    if (actions.length) {
      h += '<span style="margin-left:auto"></span>';
      actions.forEach(function (a, i) { h += '<button class="uni-btn uni-action" data-action="' + i + '">' + a.label + '</button>'; });
    }
    el.innerHTML = h;
    el.querySelector(".uni-search").oninput = function () { state.q = this.value; render(); };
    el.querySelectorAll('[data-grp="ccy"] button').forEach(function (b) {
      b.onclick = function () { state.ccy = b.dataset.v; el.querySelectorAll('[data-grp="ccy"] button').forEach(function (x) { x.classList.toggle("on", x === b); }); render(); };
    });
    el.querySelectorAll('[data-grp="tipo"] button').forEach(function (b) {
      b.onclick = function () {
        state.tipo[b.dataset.v] = !state.tipo[b.dataset.v];
        if (!state.tipo.HD && !state.tipo.DL) state.tipo[b.dataset.v] = true;
        b.classList.toggle("on", state.tipo[b.dataset.v]); render();
      };
    });
    el.querySelector('[data-act="expand"]').onclick = function () { state.colSec.clear(); state.colEmi.clear(); render(); };
    el.querySelector('[data-act="collapse"]').onclick = function () { sectorAggs().forEach(function (a) { state.colSec.add(a.key); }); render(); };
    el.querySelectorAll(".uni-action").forEach(function (b) {
      b.onclick = function () { var a = actions[+b.dataset.action]; if (a && a.onClick) a.onClick(b); };
    });
    return el;
  }

  window.Unified = {
    render: function (mount, options) {
      hostOpts = options || {};
      if (hostOpts.defaultCcy) state.ccy = hostOpts.defaultCcy;
      mount.classList.add("uni-wrap");
      var body = document.createElement("div"); body.className = "uni-body";
      mount.innerHTML = ""; mount.appendChild(toolbar()); mount.appendChild(body);
      mountEl = body;
      render();
    },
    visibleBonds: visible,
    sectorAggs: sectorAggs,
    highlight: function (k) { state.highlight = k; render(); },
    openSector: function (key) {            // expandir un sector y hacer scroll hasta él
      state.colSec.delete(key);
      render();
      var row = mountEl && mountEl.querySelector('tr.uni-sector[data-sec="' + encodeURIComponent(key) + '"]');
      if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
    },
    on: function (evt, cb) { (listeners[evt] = listeners[evt] || []).push(cb); },
    rerender: render,
    state: state,
  };
})();

/* ---- app de la página (3 subpestañas), boot por fetch('/on/data') ---- */
(function () {
  "use strict";

  /* ============================================================
     ESTADO UNIFICADO
  ============================================================ */
  var state = {
    ccy: "MEP",
    ley: { AR: true, EXT: true },
    tipo: { HD: true, DL: false },   // default: solo Hard Dollar (DL se prende a mano)
    sectors: {},
    vtoMin: null, vtoMax: null,
    tirMin: null, tirMax: null,
    parMin: null, parMax: null,
    activeTab: "sectores",
    sec:   { sortCards: "canonical", openSects: new Set(), subSorts: {}, highlighted: null, ligaSort: "tir_avg", ligaDir: -1, hmCol: "tir", hmAsc: false },
    cards: { search: "", sortKey: "tir", sortAsc: false, selected: [], seeded: false },
    dash:  { bkSort: "count", moversDir: "pos", activeSector: null, quadrant: null, medMD: 0, medTIR: 0 }
  };
  ON.SECTORS.forEach(function (s) { state.sectors[s.key] = true; });
  state.sectors["Otros"] = true;

  var MAX_SEL = 5;

  /* ============================================================
     MOTOR COMPARTIDO (de 20)
  ============================================================ */
  function vtoYear(b) { return b.vto ? parseInt(b.vto.slice(0, 4), 10) : 9999; }

  // 1 fila por ON para la moneda activa, con fallback: ARS = solo pesos; MEP/CABLE son
  // ambos USD → si falta la pata de la moneda activa se usa la otra USD. Así una ON que no
  // cotiza en esa moneda (ej. CLISA sin MEP) igual aparece, sin mezclar pesos con USD.
  var _CCY_PREF = { ARS: ["ARS"], MEP: ["MEP", "CABLE"], CABLE: ["CABLE", "MEP"] };
  function dedupLegs(ccy) {
    var pref = _CCY_PREF[ccy] || [ccy], byBase = {};
    ON.DATA.bonds.forEach(function (b) {
      var t = b.ticker || "", base = (t.length > 1 && "ODC".indexOf(t.slice(-1)) >= 0) ? t.slice(0, -1) : t;
      (byBase[base] = byBase[base] || {})[b.ccy] = b;
    });
    var out = [];
    Object.keys(byBase).forEach(function (base) {
      var L = byBase[base];
      for (var i = 0; i < pref.length; i++) { if (L[pref[i]]) { out.push(L[pref[i]]); break; } }
    });
    return out;
  }

  function filteredList() {
    return dedupLegs(state.ccy).filter(function (b) {
      if (!state.ley[b.ley]) return false;
      if (b.tipo && !state.tipo[b.tipo]) return false;
      if (!state.sectors[b.sector]) return false;
      var yr = vtoYear(b);
      if (state.vtoMin !== null && yr < state.vtoMin) return false;
      if (state.vtoMax !== null && yr > state.vtoMax) return false;
      if (state.tirMin !== null && b.tir < state.tirMin) return false;
      if (state.tirMax !== null && b.tir > state.tirMax) return false;
      if (state.parMin !== null && b.paridad < state.parMin) return false;
      if (state.parMax !== null && b.paridad > state.parMax) return false;
      return true;
    });
  }

  function buildSectorFacet() {
    var container = document.getElementById("fct-sector");
    ON.SECTORS.forEach(function (s) {
      var row = document.createElement("label");
      row.className = "facet-row";
      row.innerHTML =
        '<input type="checkbox" name="sector" value="' + s.key + '" checked>' +
        '<span class="f-dot" style="background:' + s.color + '"></span>' +
        '<span class="f-label">' + s.short + '</span>' +
        '<span class="f-count" id="cnt-sec-' + s.key.replace(/[^a-zA-Z0-9]/g, "_") + '">—</span>';
      container.appendChild(row);
    });
  }

  function updateCounts(allForCcy) {
    var noLey = allForCcy.filter(function (b) {
      if (!state.sectors[b.sector]) return false;
      var yr = vtoYear(b);
      if (state.vtoMin !== null && yr < state.vtoMin) return false;
      if (state.vtoMax !== null && yr > state.vtoMax) return false;
      if (state.tirMin !== null && b.tir < state.tirMin) return false;
      if (state.tirMax !== null && b.tir > state.tirMax) return false;
      if (state.parMin !== null && b.paridad < state.parMin) return false;
      if (state.parMax !== null && b.paridad > state.parMax) return false;
      return true;
    });
    document.getElementById("cnt-ley-ar").textContent = noLey.filter(function (b) { return b.ley === "AR"; }).length;
    document.getElementById("cnt-ley-ext").textContent = noLey.filter(function (b) { return b.ley === "EXT"; }).length;

    var noSec = allForCcy.filter(function (b) {
      if (!state.ley[b.ley]) return false;
      var yr = vtoYear(b);
      if (state.vtoMin !== null && yr < state.vtoMin) return false;
      if (state.vtoMax !== null && yr > state.vtoMax) return false;
      if (state.tirMin !== null && b.tir < state.tirMin) return false;
      if (state.tirMax !== null && b.tir > state.tirMax) return false;
      if (state.parMin !== null && b.paridad < state.parMin) return false;
      if (state.parMax !== null && b.paridad > state.parMax) return false;
      return true;
    });
    var secMap = {};
    noSec.forEach(function (b) { secMap[b.sector] = (secMap[b.sector] || 0) + 1; });
    ON.SECTORS.forEach(function (s) {
      var el = document.getElementById("cnt-sec-" + s.key.replace(/[^a-zA-Z0-9]/g, "_"));
      if (el) el.textContent = secMap[s.key] || 0;
    });

    // conteo Tipo (HD/DL) respetando el resto de los filtros (no el de tipo)
    var hd = 0, dl = 0;
    allForCcy.forEach(function (b) {
      if (!state.ley[b.ley]) return;
      if (!state.sectors[b.sector]) return;
      var yr = vtoYear(b);
      if (state.vtoMin !== null && yr < state.vtoMin) return;
      if (state.vtoMax !== null && yr > state.vtoMax) return;
      if (state.tirMin !== null && b.tir < state.tirMin) return;
      if (state.tirMax !== null && b.tir > state.tirMax) return;
      if (state.parMin !== null && b.paridad < state.parMin) return;
      if (state.parMax !== null && b.paridad > state.parMax) return;
      if (b.tipo === "HD") hd++; else if (b.tipo === "DL") dl++;
    });
    var elHd = document.getElementById("cnt-tipo-hd"); if (elHd) elHd.textContent = hd;
    var elDl = document.getElementById("cnt-tipo-dl"); if (elDl) elDl.textContent = dl;
  }

  function renderKPIs(list) {
    if (!document.getElementById("kpi-n")) return;   // barra de KPIs removida → no-op
    var tirs = list.map(function (b) { return b.tir; }).filter(function (v) { return v != null && !isNaN(v); });
    var mds  = list.map(function (b) { return b.md; }).filter(function (v) { return v != null && !isNaN(v) && v > 0; });
    var pars = list.map(function (b) { return b.paridad; }).filter(function (v) { return v != null && !isNaN(v); });
    var vols = list.map(function (b) { return b.volume || 0; });
    document.getElementById("kpi-n").textContent = list.length;
    document.getElementById("kpi-ar").textContent = list.filter(function (b) { return b.ley === "AR"; }).length;
    document.getElementById("kpi-ext").textContent = list.filter(function (b) { return b.ley === "EXT"; }).length;
    var tirMed = ON.median(tirs), tirAvg = ON.avg(tirs);
    document.getElementById("kpi-tir-med").textContent = tirMed != null ? ON.num(tirMed, 2) + "%" : "—";
    document.getElementById("kpi-tir-avg").textContent = tirAvg != null ? ON.num(tirAvg, 2) + "%" : "—";
    var mdMed = ON.median(mds), mdAvg = ON.avg(mds);
    document.getElementById("kpi-md-med").textContent = mdMed != null ? ON.num(mdMed, 2) + "a" : "—";
    document.getElementById("kpi-md-avg").textContent = mdAvg != null ? ON.num(mdAvg, 2) + "a" : "—";
    var parMed = ON.median(pars);
    var pMin = pars.length ? Math.min.apply(null, pars) : null;
    var pMax = pars.length ? Math.max.apply(null, pars) : null;
    document.getElementById("kpi-par-med").textContent = parMed != null ? ON.num(parMed, 1) + "%" : "—";
    document.getElementById("kpi-par-range").textContent = (pMin != null && pMax != null) ? ON.num(pMin, 0) + "–" + ON.num(pMax, 0) : "—";
    document.getElementById("kpi-vol").textContent = ON.vol(ON.sum(vols));
    document.getElementById("kpi-vol-sub").textContent = "HOY · " + state.ccy;
  }

  function renderChips(allForCcy) {
    var bar = document.getElementById("chips-bar");
    while (bar.firstChild) bar.removeChild(bar.firstChild);
    var chips = [];
    chips.push({ label: state.ccy, clear: null });
    var leyKeys = Object.keys(state.ley).filter(function (k) { return state.ley[k]; });
    if (leyKeys.length === 1) chips.push({ label: "Ley " + leyKeys[0], clear: "ley" });
    var allSecKeys = ON.SECTORS.map(function (s) { return s.key; }).concat(["Otros"]);
    var activeSec = allSecKeys.filter(function (k) { return state.sectors[k]; });
    var inactiveSec = allSecKeys.filter(function (k) { return !state.sectors[k]; });
    if (inactiveSec.length > 0 && activeSec.length <= 3) {
      activeSec.forEach(function (k) { var sm = ON.sectorMeta(k); chips.push({ label: sm.short, color: sm.color, clear: null }); });
    } else if (inactiveSec.length > 0) {
      chips.push({ label: activeSec.length + " sectores", clear: "sector" });
    }
    if (state.vtoMin !== null) chips.push({ label: "Vto ≥ " + state.vtoMin, clear: "vtoMin" });
    if (state.vtoMax !== null) chips.push({ label: "Vto ≤ " + state.vtoMax, clear: "vtoMax" });
    if (state.tirMin !== null) chips.push({ label: "TIR ≥ " + state.tirMin + "%", clear: "tirMin" });
    if (state.tirMax !== null) chips.push({ label: "TIR ≤ " + state.tirMax + "%", clear: "tirMax" });
    if (state.parMin !== null) chips.push({ label: "Par ≥ " + state.parMin + "%", clear: "parMin" });
    if (state.parMax !== null) chips.push({ label: "Par ≤ " + state.parMax + "%", clear: "parMax" });
    chips.forEach(function (c) {
      var el = document.createElement("span");
      el.className = "active-chip";
      if (c.color) el.innerHTML = '<span class="f-dot" style="background:' + c.color + ';width:8px;height:8px;border-radius:50%;flex-shrink:0"></span>' + c.label;
      else el.textContent = c.label;
      if (c.clear) {
        var xb = document.createElement("span"); xb.className = "x-btn"; xb.textContent = "×"; xb.title = "Quitar filtro";
        (function (k) { xb.addEventListener("click", function () { clearFacet(k); }); })(c.clear);
        el.appendChild(xb);
      }
      bar.appendChild(el);
    });
    var note = document.createElement("span"); note.className = "chips-note";
    note.textContent = "(" + filteredList().length + " de " + allForCcy.length + ")";
    bar.appendChild(note);
  }

  function clearFacet(key) {
    if (key === "ley") { state.ley = { AR: true, EXT: true }; document.querySelectorAll("input[name='ley']").forEach(function (el) { el.checked = true; }); }
    else if (key === "sector") { ON.SECTORS.forEach(function (s) { state.sectors[s.key] = true; }); state.sectors["Otros"] = true; document.querySelectorAll("input[name='sector']").forEach(function (el) { el.checked = true; }); }
    else if (key === "tipo") { state.tipo = { HD: true, DL: true }; document.querySelectorAll("input[name='tipo']").forEach(function (el) { el.checked = true; }); }
    else if (key === "vtoMin") { state.vtoMin = null; document.getElementById("vto-min").value = ""; }
    else if (key === "vtoMax") { state.vtoMax = null; document.getElementById("vto-max").value = ""; }
    else if (key === "vto") { state.vtoMin = state.vtoMax = null; document.getElementById("vto-min").value = ""; document.getElementById("vto-max").value = ""; }
    else if (key === "tirMin") { state.tirMin = null; document.getElementById("tir-min").value = ""; }
    else if (key === "tirMax") { state.tirMax = null; document.getElementById("tir-max").value = ""; }
    else if (key === "tir") { state.tirMin = state.tirMax = null; document.getElementById("tir-min").value = ""; document.getElementById("tir-max").value = ""; }
    else if (key === "parMin") { state.parMin = null; document.getElementById("par-min").value = ""; }
    else if (key === "parMax") { state.parMax = null; document.getElementById("par-max").value = ""; }
    else if (key === "par") { state.parMin = state.parMax = null; document.getElementById("par-min").value = ""; document.getElementById("par-max").value = ""; }
    refresh();
  }

  /* ============================================================
     GLUE: registro de charts + dispatch de tabs
  ============================================================ */
  var charts = { sectores: [], cards: [], dash: [] };
  function trackChart(tab, c) { charts[tab].push(c); return c; }
  var TAB_TEARDOWN = {
    sectores: function () { uniChartDestroy(); cardMinis.forEach(function (c) { try { c.destroy(); } catch (e) {} }); cardMinis = []; },
    cards: function () { cmpDestroy(); }
  };
  function destroyTab(tab) {
    charts[tab].forEach(function (c) { try { c.destroy(); } catch (e) {} });
    charts[tab] = [];
    if (TAB_TEARDOWN[tab]) TAB_TEARDOWN[tab]();
  }
  var TAB_RENDER = { sectores: renderSectores, cards: renderCards, dash: renderDash };
  function renderActiveTab() { TAB_RENDER[state.activeTab](); }
  function switchTab(tab) {
    if (tab === state.activeTab) return;
    destroyTab(state.activeTab);
    state.activeTab = tab;
    document.querySelectorAll(".subtab").forEach(function (b) { b.classList.toggle("on", b.dataset.tab === tab); });
    document.querySelectorAll(".tab-pane").forEach(function (p) { p.hidden = (p.id !== "tab-" + tab); });
    renderActiveTab();
  }

  function refresh() {
    var allForCcy = dedupLegs(state.ccy);
    updateCounts(allForCcy);
    renderKPIs(filteredList());
    renderChips(allForCcy);
    renderActiveTab();
    document.getElementById("footer-note").textContent =
      "Datos: " + (ON.DATA.generated || "—") + " · " + filteredList().length + " resultados · " + state.ccy +
      " · vista: " + state.activeTab + " · O.N's Monitor Balanz";
  }

  /* ============================================================
     TAB 1 — SECTORES (herramienta unificada + modal del gráfico)
  ============================================================ */
  // La tabla Sector›Emisor›Título la monta window.Unified (de _shared/unified.js),
  // alimentada por filteredList() (el sidebar de facetas global). El gráfico TIR-vs-MD
  // por sector vive en un modal que se abre desde la toolbar de la herramienta.
  var uniMounted = false;
  var uniChart = null;     // instancia Chart del modal
  var uniAggs = [];        // últimos agregados por sector (redibujo/recolor)
  var uniSel = null;       // sector resaltado (toggle)

  function uniRadius(count) { return Math.max(8, Math.min(28, 6 + count * 1.3)); }

  function uniChartDatasets(aggs) {
    return (aggs || []).filter(function (a) { return a.md_avg != null && a.tir_avg != null && a.count > 0; })
      .map(function (a) {
        return {
          label: a.short,
          data: [{ x: a.md_avg, y: a.tir_avg, _key: a.key, _short: a.short, _count: a.count, _tir: a.tir_avg, _md: a.md_avg }],
          backgroundColor: a.color + "cc", borderColor: a.color, borderWidth: 2,
          radius: uniRadius(a.count), hoverRadius: uniRadius(a.count) + 3
        };
      });
  }

  function uniChartOptions() {
    var t = ON.chartTheme();
    return {
      responsive: true, maintainAspectRatio: false, animation: { duration: 260 },
      onClick: function (evt, els) {
        if (!els || !els.length) return;
        var d = uniChart.data.datasets[els[0].datasetIndex];
        var key = d.data[0]._key;
        uniSel = (uniSel === key) ? null : key;
        Unified.highlight(uniSel);
        uniUpdateSelNote();
      },
      scales: {
        x: { title: { display: true, text: "MD prom (años)", color: t.txt }, ticks: { color: t.txt }, grid: { color: t.grid }, border: { color: t.grid } },
        y: { title: { display: true, text: "TIR prom (%)", color: t.txt }, ticks: { color: t.txt, callback: function (v) { return v + "%"; } }, grid: { color: t.grid }, border: { color: t.grid } }
      },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: function (c) { var r = c.raw || {}; return r._short + " (" + r._count + " bonos) · TIR prom " + (r._tir != null ? r._tir.toFixed(2) : "?") + "% · MD prom " + (r._md != null ? r._md.toFixed(2) : "?") + " a"; } } }
      }
    };
  }

  function uniEnsureChart() {
    if (uniChart || !window.Chart) return;
    var cv = document.getElementById("uni-chart");
    if (!cv) return;
    uniChart = trackChart("sectores", new Chart(cv.getContext("2d"), { type: "bubble", data: { datasets: uniChartDatasets(uniAggs) }, options: uniChartOptions() }));
  }
  function uniChartDestroy() { if (uniChart) { try { uniChart.destroy(); } catch (e) {} uniChart = null; } }

  function uniRedrawChart(aggs) {
    if (aggs) uniAggs = aggs;
    if (!uniChart) { uniEnsureChart(); return; }
    uniChart.data.datasets = uniChartDatasets(uniAggs);
    uniChart.options = uniChartOptions();
    uniChart.update();
  }

  function uniUpdateSelNote() {
    var note = document.getElementById("uni-chart-selnote");
    var sub = document.getElementById("uni-chart-sub");
    if (uniSel) {
      var m = ON.sectorMeta(uniSel);
      if (note) note.innerHTML = '· resaltado: <b style="color:' + m.color + '">' + m.short + '</b> (click de nuevo para limpiar)';
      if (sub) sub.textContent = m.short;
    } else {
      if (note) note.textContent = "";
      if (sub) sub.textContent = "";
    }
  }

  function openUniModal() {
    var bd = document.getElementById("uni-chart-backdrop");
    bd.classList.add("open"); bd.setAttribute("aria-hidden", "false");
    uniEnsureChart();
    if (uniChart) requestAnimationFrame(function () { uniChart.resize(); uniChart.update("none"); });
    // a11y: focus-trap + restaurar foco al botón "Ver gráfico" (helper de base.html;
    // el guard mantiene el mock standalone — sin base.html — funcionando).
    if (window.A11yModal) A11yModal.open(bd.querySelector(".uni-modal") || bd,
      { focus: document.getElementById("uni-chart-close"), onEsc: closeUniModal });
  }
  function closeUniModal() {
    var bd = document.getElementById("uni-chart-backdrop");
    bd.classList.remove("open"); bd.setAttribute("aria-hidden", "true");
    if (window.A11yModal) A11yModal.close(bd.querySelector(".uni-modal") || bd);
  }

  // ----- Tarjetas por sector (colapsables) — overview arriba de la herramienta -----
  var cardMinis = [];

  function secSortedSectors(secMap) {
    var entries = [];
    secMap.forEach(function (bonds, key) {
      var tirs = bonds.map(function (b) { return b.tir; }).filter(function (v) { return v != null && !isNaN(v); });
      var mds = bonds.map(function (b) { return b.md; }).filter(function (v) { return v != null && v > 0; });
      var vols = bonds.map(function (b) { return b.volume || 0; });
      entries.push({ key: key, bonds: bonds, tir_avg: ON.avg(tirs), md_avg: ON.avg(mds), vol_total: ON.sum(vols), count: bonds.length });
    });
    var fn;
    switch (state.sec.sortCards) {
      case "tir_desc":   fn = function (a, b) { return (b.tir_avg || 0) - (a.tir_avg || 0); }; break;
      case "tir_asc":    fn = function (a, b) { return (a.tir_avg || 0) - (b.tir_avg || 0); }; break;
      case "md_desc":    fn = function (a, b) { return (b.md_avg || 0) - (a.md_avg || 0); }; break;
      case "vol_desc":   fn = function (a, b) { return (b.vol_total || 0) - (a.vol_total || 0); }; break;
      case "count_desc": fn = function (a, b) { return b.count - a.count; }; break;
      default:
        var order = {}; ON.SECTORS.forEach(function (s, i) { order[s.key] = i; });
        fn = function (a, b) { return (order[a.key] == null ? 99 : order[a.key]) - (order[b.key] == null ? 99 : order[b.key]); };
    }
    return entries.sort(fn);
  }

  function secTopHtml(bonds, sortKey) {
    var sorted = bonds.slice().sort(function (a, b) {
      if (sortKey === "vol") return (b.volume || 0) - (a.volume || 0);
      // Usar null-coalesce explícito: tir=0.0 es legítimo, no debe caer a -Infinity.
      return (b.tir != null ? b.tir : -Infinity) - (a.tir != null ? a.tir : -Infinity);
    }).slice(0, 5);
    var valLabel = sortKey === "vol" ? "Vol" : "TIR";
    var hdr = '<div class="top3-header"><span class="top3-num">#</span><span class="top3-tk">Ticker</span><span class="top3-cl">Clase</span><span class="top3-ley">Ley</span><span class="top3-em">Emisor</span><span class="top3-val">' + valLabel + '</span></div>';
    return hdr + sorted.map(function (b, i) {
      var valStr = sortKey === "vol" ? ON.vol(b.volume) : ON.pct(b.tir);
      var valClass = sortKey === "vol" ? "" : ON.sign(b.tir);
      var em = (b.emisor || "").replace(/\s*-\s*Clase.*$/i, "").replace(/\s*S\.A\..*$/i, "").trim();
      var ley = b.ley === "AR" ? '<span class="top3-ley ley-ar">AR</span>' : '<span class="top3-ley ley-ext">EXT</span>';
      var cl = b.clase ? '<span class="top3-cl">' + ON.esc(b.clase) + '</span>' : '<span class="top3-cl dim">—</span>';
      return '<div class="top3-row"><span class="top3-num">' + (i + 1) + '</span><span class="top3-tk">' + ON.esc(b.ticker) + '</span>' + cl + ley +
        '<span class="top3-em">' + ON.esc(em) + '</span><span class="top3-val ' + valClass + '">' + valStr + '</span></div>';
    }).join("");
  }

  function secBuildMini(canvasId, bonds, color) {
    var canvas = document.getElementById(canvasId);
    if (!canvas || window.Chart === undefined) return;
    var pts = ON.scatterPoints(bonds);
    var t = ON.chartTheme();
    var fit = ON.logFit(pts);
    var datasets = [];
    if (fit && pts.length >= 3) {
      var xs = pts.map(function (p) { return p.x; });
      var xmin = Math.min.apply(null, xs) * 0.9, xmax = Math.max.apply(null, xs) * 1.1;
      if (xmin <= 0) xmin = 0.01;
      datasets.push({ type: "line", data: fit.curve(xmin, xmax, 30), borderColor: color + "88", borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.4, parsing: false });
    }
    var arPts = pts.filter(function (p) { return p.ley === "AR"; });
    var extPts = pts.filter(function (p) { return p.ley === "EXT"; });
    if (arPts.length) datasets.push({ type: "scatter", data: arPts, backgroundColor: color + "cc", borderColor: color, borderWidth: 1.2, pointRadius: 4, pointHoverRadius: 6, parsing: false });
    if (extPts.length) datasets.push({ type: "scatter", data: extPts, backgroundColor: color + "55", borderColor: color, borderWidth: 1.2, pointRadius: 4, pointHoverRadius: 6, pointStyle: "triangle", parsing: false });
    if (!datasets.length) { canvas.style.display = "none"; return; }
    var opts = {
      responsive: true, maintainAspectRatio: false, animation: false,
      scales: {
        x: { ticks: { color: t.txt, font: { size: 9 }, maxTicksLimit: 5 }, grid: { color: t.grid + "88" } },
        y: { ticks: { color: t.txt, font: { size: 9 }, maxTicksLimit: 5, callback: function (v) { return v.toFixed(0) + "%"; } }, grid: { color: t.grid + "88" } }
      },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: function (c) { var r = c.raw || {}; return (r.t || "") + "  MD " + (r.x != null ? r.x.toFixed(2) : "?") + "  TIR " + (r.y != null ? r.y.toFixed(2) : "?") + "%"; } } }
      }
    };
    cardMinis.push(new Chart(canvas, { type: "scatter", data: { datasets: datasets }, options: opts }));
  }

  function secRenderCards() {
    cardMinis.forEach(function (c) { try { c.destroy(); } catch (e) {} }); cardMinis = [];
    var list = filteredList();
    var entries = secSortedSectors(ON.bySector(list));
    var grid = document.getElementById("sec-cards");
    grid.innerHTML = "";
    if (!entries.length) { grid.innerHTML = '<div style="color:var(--text-faint);padding:30px;text-align:center;grid-column:1/-1">Sin datos para la selección actual.</div>'; return; }
    entries.forEach(function (entry, i) {
      var meta = ON.sectorMeta(entry.key), bonds = entry.bonds, color = meta.color;
      var ar = bonds.filter(function (b) { return b.ley === "AR"; }).length;
      var ext = bonds.filter(function (b) { return b.ley === "EXT"; }).length;
      var canvasId = "scard-cv-" + i, top3Id = "scard-top3-" + i;
      var card = document.createElement("div");
      card.className = "scard";
      card.innerHTML =
        '<div class="scard-bar" style="background:' + color + '"></div>' +
        '<div class="scard-head"><span class="scard-icon">' + meta.icon + '</span>' +
          '<span class="scard-name" style="color:' + color + '">' + meta.short + '</span>' +
          '<span class="scard-counts">' +
            (ar ? '<span class="cnt-pill cnt-ar">AR ' + ar + '</span>' : '') +
            (ext ? '<span class="cnt-pill cnt-ext">EXT ' + ext + '</span>' : '') +
            '<span class="cnt-pill cnt-all">' + entry.count + ' ONs</span>' +
          '</span></div>' +
        '<div class="scard-kpis">' +
          '<div class="scard-kpi"><div class="sk-label">TIR prom</div><div class="sk-val ' + ON.sign(entry.tir_avg) + '">' + ON.pct(entry.tir_avg) + '</div><div class="sk-sub">anual</div></div>' +
          '<div class="scard-kpi"><div class="sk-label">MD prom</div><div class="sk-val">' + (entry.md_avg != null ? ON.num(entry.md_avg) : "—") + '</div><div class="sk-sub">años</div></div>' +
          '<div class="scard-kpi"><div class="sk-label">Vol hoy</div><div class="sk-val">' + ON.vol(entry.vol_total) + '</div><div class="sk-sub">nominal</div></div>' +
        '</div>' +
        '<div class="scard-chart"><canvas id="' + canvasId + '"></canvas></div>' +
        '<div class="scard-top"><div class="top3-head">Top 5<div class="top3-tabs"><button class="top3-tab on" data-s="tir">TIR</button><button class="top3-tab" data-s="vol">Vol</button></div></div><div id="' + top3Id + '">' + secTopHtml(bonds, "tir") + '</div></div>' +
        '<button class="expand-btn" data-sec="' + entry.key + '"><span>Ver bonos ↓</span></button>';
      grid.appendChild(card);
      card.querySelectorAll(".top3-tab").forEach(function (tab) {
        tab.addEventListener("click", function () {
          card.querySelectorAll(".top3-tab").forEach(function (x) { x.classList.toggle("on", x === tab); });
          document.getElementById(top3Id).innerHTML = secTopHtml(bonds, tab.dataset.s);
        });
      });
      card.querySelector(".expand-btn").addEventListener("click", function () { secOpenLiga(entry.key); });
      (function (id, b, c) { requestAnimationFrame(function () { secBuildMini(id, b, c); }); })(canvasId, bonds, color);
    });
  }

  // "Ver bonos ↓" → expandir ese sector en la herramienta unificada y hacer scroll
  function secOpenLiga(key) { if (window.Unified && Unified.openSector) Unified.openSector(key); }

  // Botón 🖨️ de la toolbar: baja el PDF (resumen por sector + detalle) desde /on/pdf,
  // el MISMO que arma scripts/export_on_pdf.py. Muestra estado de carga (tarda unos seg
  // por los scatter de matplotlib del server).
  // POSTea los tickers visibles (filteredList) para que el PDF respete las facetas.
  function downloadOnPdf(btn) {
    var orig = btn ? btn.textContent : null;
    if (btn) { btn.textContent = "⏳ Generando…"; btn.disabled = true; }
    fetch("/on/pdf", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tickers: filteredList().map(function (b) { return b.ticker; }) })
    })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.blob(); })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url; a.download = "Obligaciones_Negociables.pdf";
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
      })
      .catch(function (e) { alert("No se pudo generar el PDF: " + e.message); })
      .finally(function () { if (btn) { btn.textContent = orig; btn.disabled = false; } });
  }

  function renderSectores() {
    secRenderCards();
    if (!uniMounted) {
      Unified.render(document.getElementById("uni-tool"), {
        bondsFn: filteredList,                                  // sigue el sidebar de facetas global
        actions: [{ label: "🖨️ PDF", onClick: downloadOnPdf }, { label: "📊 Ver gráfico", onClick: openUniModal }],
        onRender: function (bonds, aggs) {
          if (uniSel && !aggs.some(function (a) { return a.key === uniSel; })) { uniSel = null; uniUpdateSelNote(); }
          uniRedrawChart(aggs);
        }
      });
      uniMounted = true;
    } else {
      Unified.rerender();   // re-aplica filtros + redibuja el gráfico (vía onRender)
    }
  }

  /* ============================================================
     TAB 2 — CARDS + COMPARADOR
  ============================================================ */
  var cardBgCache = {};
  function cardGetBg(ccy) { if (!cardBgCache[ccy]) cardBgCache[ccy] = ON.scatterPoints(ON.legs(ccy)); return cardBgCache[ccy]; }

  function renderCards() {
    destroyTab("cards");
    cardRenderDeck();
    cmpRender();
  }

  function cardRenderDeck() {
    var base = filteredList();
    var q = state.cards.search.trim().toLowerCase();
    var list = base.filter(function (b) {
      if (!q) return true;
      return b.ticker.toLowerCase().indexOf(q) !== -1 || (b.emisor || "").toLowerCase().indexOf(q) !== -1;
    });
    var k = state.cards.sortKey, asc = state.cards.sortAsc;
    list = list.slice().sort(function (a, b) {
      var av = a[k], bv = b[k];
      if (typeof av === "string") av = av.toLowerCase();
      if (typeof bv === "string") bv = bv.toLowerCase();
      if (av == null) return 1; if (bv == null) return -1;
      return asc ? (av < bv ? -1 : av > bv ? 1 : 0) : (av > bv ? -1 : av < bv ? 1 : 0);
    });
    document.getElementById("deck-count").textContent = list.length + " / " + base.length + " ONs";
    var allPts = cardGetBg(state.ccy);
    var grid = document.getElementById("deck-grid");
    grid.innerHTML = "";
    if (!list.length) { grid.innerHTML = '<div class="deck-empty">Sin bonos para los filtros seleccionados.</div>'; return; }
    list.forEach(function (bond) { grid.appendChild(cardBuildCard(bond, allPts)); });
  }

  function cardBuildMini(canvas, bond, allPts) {
    var t = ON.chartTheme(), sc = ON.sectorColor(bond.sector);
    var bgDs = { data: allPts, pointRadius: 2.3, pointHoverRadius: 2.3, backgroundColor: t.grid, borderWidth: 0, showLine: false };
    var thisDs = { data: (bond.md != null && bond.tir != null) ? [{ x: bond.md, y: bond.tir, t: bond.ticker }] : [], pointRadius: 6, pointHoverRadius: 7, backgroundColor: sc, borderColor: "#fff", borderWidth: 1.5, showLine: false };
    trackChart("cards", new Chart(canvas, {
      type: "scatter",
      data: { datasets: [bgDs, thisDs] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false }, tooltip: { filter: function (it) { return it.datasetIndex === 1; }, callbacks: { label: function (ctx) { var r = ctx.raw || {}; return ctx.datasetIndex === 1 && r.t ? r.t + "  MD " + (r.x != null ? r.x.toFixed(2) : "?") + "  TIR " + (r.y != null ? r.y.toFixed(2) : "?") + "%" : null; } } } },
        scales: { x: { display: false, grid: { display: false } }, y: { display: false, grid: { display: false } } }
      }
    }));
  }

  function cardBuildCard(bond, allPts) {
    var sm = ON.sectorMeta(bond.sector), sc = sm.color;
    var card = document.createElement("div");
    card.className = "bond-card" + (state.cards.selected.indexOf(bond.ticker) !== -1 ? " selected" : "");
    card.dataset.tk = bond.ticker;
    var tirNum = (bond.tir != null && !isNaN(bond.tir)) ? bond.tir : null;
    var tirColor = tirNum == null ? "" : (tirNum >= 0 ? "var(--pos)" : "var(--neg)");
    var em = (bond.emisor || "");
    em = em.length > 32 ? em.replace(/ [-–] Clase .*$/, "").substring(0, 34) : em;
    var ch = bond.change_pct;
    var chHtml = (ch != null && !isNaN(ch)) ? '<span class="card-change ' + (ch >= 0 ? "pos" : "neg") + '">' + (ch >= 0 ? "+" : "") + ch.toFixed(2) + '%</span>' : '<span class="card-change dim">—</span>';
    card.innerHTML =
      '<span class="card-pick">✓</span>' +
      '<div class="card-stripe" style="background:' + sc + '"></div>' +
      '<div class="card-top"><div class="card-ticker">' + ON.esc(bond.ticker) + '</div><div class="card-badges"><span class="badge-ley ' + bond.ley.toLowerCase() + '">' + bond.ley + '</span><span class="sector-dot" style="background:' + sc + '" title="' + ON.esc(sm.short) + '"></span></div></div>' +
      '<div class="card-emisor" title="' + ON.esc(bond.emisor) + '">' + ON.esc(em) + '</div>' +
      '<div class="card-tir-row"><span class="card-tir-val" style="color:' + tirColor + '">' + (tirNum != null ? tirNum.toFixed(2) + "%" : "—") + '</span><span class="card-tir-lbl">TIR</span></div>' +
      '<div class="card-mini-wrap"><canvas class="card-mini-canvas"></canvas></div>' +
      '<div class="card-metrics"><div class="card-metric"><span class="m-lbl">MD</span><span class="m-val">' + (bond.md != null ? bond.md.toFixed(2) + "a" : "—") + '</span></div><div class="card-metric"><span class="m-lbl">Paridad</span><span class="m-val">' + (bond.paridad != null ? bond.paridad.toFixed(1) + "%" : "—") + '</span></div><div class="card-metric"><span class="m-lbl">Días</span><span class="m-val">' + (bond.dias != null ? bond.dias + "d" : "—") + '</span></div></div>' +
      '<div class="card-footer">' + chHtml + '<span class="card-vol">Vol ' + ON.vol(bond.volume) + '</span><span class="card-dias">' + ON.date(bond.vto) + '</span></div>';
    var canvas = card.querySelector(".card-mini-canvas");
    requestAnimationFrame(function () { cardBuildMini(canvas, bond, allPts); });
    return card;
  }

  function cardToggleSelect(tk) {
    var idx = state.cards.selected.indexOf(tk);
    if (idx !== -1) state.cards.selected.splice(idx, 1);
    else { if (state.cards.selected.length >= MAX_SEL) return; state.cards.selected.push(tk); }
    var card = document.querySelector('#deck-grid .bond-card[data-tk="' + tk + '"]');
    if (card) card.classList.toggle("selected", state.cards.selected.indexOf(tk) !== -1);
    cmpRender();
  }

  // ----- comparador (de 11) -----
  var cmpCharts = [];
  function cmpDestroy() { cmpCharts.forEach(function (c) { try { c.destroy(); } catch (e) {} }); cmpCharts = []; }
  function cmpTruncate(s, n) { if (!s) return ""; return s.length > n ? s.slice(0, n - 1) + "…" : s; }
  function cmpHexAlpha(hex, a) { var r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16); return "rgba(" + r + "," + g + "," + b + "," + a + ")"; }
  function cmpSelectedBonds() {
    var all = ON.legs(state.ccy);
    return state.cards.selected.map(function (tk) { return all.filter(function (b) { return b.ticker === tk; })[0]; }).filter(Boolean);
  }

  var CMP_ROWS = [
    { key: "tir", label: "TIR (%)", fmt: function (v) { return ON.pct(v); }, higher: true },
    { key: "md", label: "Dur. (años)", fmt: function (v) { return ON.num(v); }, higher: false },
    { key: "paridad", label: "Paridad (%)", fmt: function (v) { return ON.pct(v); }, higher: true },
    { key: "price", label: "Precio", fmt: function (v) { return ON.num(v); }, higher: false },
    { key: "change_pct", label: "% Día", fmt: function (v) { return ON.pctSigned(v); }, higher: true },
    { key: "volume", label: "Volumen", fmt: function (v) { return ON.vol(v); }, higher: true },
    { key: "vto", label: "Vto", fmt: function (v) { return ON.date(v); }, higher: false },
    { key: "dias", label: "Días", fmt: function (v) { return ON.num(v, 0); }, higher: false },
    { key: "ley", label: "Ley", fmt: function (v) { return v; }, higher: false },
    { key: "sector", label: "Sector", fmt: function (v) { return v; }, higher: false }
  ];

  function cmpRender() {
    cmpDestroy();
    var bonds = cmpSelectedBonds();
    document.getElementById("cmp-count").innerHTML = "<strong>" + state.cards.selected.length + "</strong> / " + MAX_SEL;
    // chips
    var chipsArea = document.getElementById("cmp-chips-area");
    if (!state.cards.selected.length) chipsArea.innerHTML = '<div class="sel-chips-empty">Ninguna ON seleccionada aún</div>';
    else chipsArea.innerHTML = '<div class="sel-chips">' + state.cards.selected.map(function (tk) {
      var b = bonds.filter(function (x) { return x.ticker === tk; })[0];
      var color = b ? ON.sectorColor(b.sector) : "#8993b8";
      return '<span class="sel-chip" data-rm="' + tk + '"><span class="chip-dot" style="background:' + color + '"></span>' + tk + '<span class="chip-x">✕</span></span>';
    }).join("") + '</div>';
    // table
    var tArea = document.getElementById("cmp-table-area");
    if (bonds.length < 2) {
      tArea.innerHTML = '<div class="cmp-empty"><div class="cmp-empty-ico">⚖️</div><p>Elegí al menos 2 ONs (clic en las cards) para comparar</p></div>';
    } else {
      var html = '<table class="cmp"><thead><tr><th class="row-lbl" style="text-align:left">Métrica</th>';
      bonds.forEach(function (b) {
        var sm = ON.sectorMeta(b.sector);
        html += '<th style="border-top:3px solid ' + sm.color + '"><span class="cmp-ticker-hd">' + b.ticker + '</span></th>';
      });
      html += '</tr></thead><tbody>';
      CMP_ROWS.forEach(function (row) {
        var vals = bonds.map(function (b) { return b[row.key]; });
        var bestIdx = -1, worstIdx = -1;
        if (row.higher) {
          var nums = vals.map(parseFloat), maxV = -Infinity, minV = Infinity;
          nums.forEach(function (v, i) { if (!isNaN(v)) { if (v > maxV) { maxV = v; bestIdx = i; } if (v < minV) { minV = v; worstIdx = i; } } });
          if (bestIdx === worstIdx) { bestIdx = -1; worstIdx = -1; }
        }
        html += '<tr><td class="row-lbl">' + row.label + '</td>';
        vals.forEach(function (v, i) {
          var cls = "num";
          if (i === bestIdx) cls += " best"; else if (i === worstIdx) cls += " worst";
          var disp = row.fmt(v);
          if (row.key === "sector") { disp = '<span class="col-sector-bar" style="background:' + ON.sectorColor(v) + '"></span>' + ON.sectorMeta(v).short; cls = ""; }
          if (row.key === "ley") { disp = '<span class="pr-ley ' + (v || "").toLowerCase() + '">' + v + '</span>'; cls = ""; }
          if (row.key === "change_pct") cls += " " + ON.sign(v);
          html += '<td class="' + cls + '">' + disp + '</td>';
        });
        html += '</tr>';
      });
      html += '</tbody></table>';
      tArea.innerHTML = html;
    }
    // charts
    var chartsArea = document.getElementById("cmp-charts-area");
    if (!bonds.length) { chartsArea.innerHTML = ""; return; }
    chartsArea.innerHTML =
      '<div class="cmp-charts"><div><div class="cmp-chart-title">TIR (%)</div><canvas id="cmp-bar-tir"></canvas></div><div><div class="cmp-chart-title">MD (años)</div><canvas id="cmp-bar-md"></canvas></div></div>' +
      '<div class="cmp-scatter-wrap"><div class="cmp-chart-title">TIR vs MD · selección + universo filtrado</div><canvas id="cmp-scatter"></canvas></div>';
    var t = ON.chartTheme();
    var barColors = bonds.map(function (b) { return ON.sectorColor(b.sector); });
    cmpCharts.push(new Chart(document.getElementById("cmp-bar-tir"), {
      type: "bar", data: { labels: bonds.map(function (b) { return b.ticker; }), datasets: [{ data: bonds.map(function (b) { return b.tir; }), backgroundColor: barColors, borderRadius: 4, borderSkipped: false }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (c) { return ON.pct(c.parsed.y); } } } }, scales: { x: { ticks: { color: t.txt }, grid: { color: t.grid } }, y: { ticks: { color: t.txt, callback: function (v) { return v.toFixed(1) + "%"; } }, grid: { color: t.grid } } } }
    }));
    cmpCharts.push(new Chart(document.getElementById("cmp-bar-md"), {
      type: "bar", data: { labels: bonds.map(function (b) { return b.ticker; }), datasets: [{ data: bonds.map(function (b) { return b.md; }), backgroundColor: barColors, borderRadius: 4, borderSkipped: false }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (c) { return ON.num(c.parsed.y) + " años"; } } } }, scales: { x: { ticks: { color: t.txt }, grid: { color: t.grid } }, y: { ticks: { color: t.txt, callback: function (v) { return v.toFixed(2); } }, grid: { color: t.grid } } } }
    }));
    cmpRenderScatter(bonds);
  }

  function cmpTickerLabels(selBonds) {
    return {
      id: "cmp-ticker-labels",
      afterDatasetsDraw: function (chart) {
        var ctx = chart.ctx, n = chart.data.datasets.length, nBg = n - selBonds.length;
        ctx.save(); ctx.font = "bold 11px monospace"; ctx.textAlign = "center";
        selBonds.forEach(function (b, i) {
          var ds = chart.getDatasetMeta(nBg + i);
          if (!ds || !ds.data || !ds.data[0]) return;
          var pt = ds.data[0];
          ctx.fillStyle = ON.sectorMeta(b.sector).color;
          ctx.fillText(b.ticker, pt.x, pt.y - 14);
        });
        ctx.restore();
      }
    };
  }

  function cmpRenderScatter(selBonds) {
    var selTk = {}; state.cards.selected.forEach(function (tk) { selTk[tk] = true; });
    var bg = filteredList().filter(function (b) { return !selTk[b.ticker]; });
    var bgDatasets = [];
    ON.bySector(bg).forEach(function (bonds, skey) {
      var pts = ON.scatterPoints(bonds);
      if (!pts.length) return;
      bgDatasets.push({ label: ON.sectorMeta(skey).short, data: pts, backgroundColor: cmpHexAlpha(ON.sectorColor(skey), 0.20), pointRadius: 3.5, pointHoverRadius: 5, order: 10 });
    });
    var selDatasets = selBonds.map(function (b) {
      return { label: b.ticker, data: ON.scatterPoints([b]), backgroundColor: ON.sectorMeta(b.sector).color, borderColor: "#fff", borderWidth: 2, pointRadius: 9, pointHoverRadius: 11, order: 1 };
    });
    var opts = ON.baseScatterOpts({ xLabel: "MD (años)", yLabel: "TIR (%)" });
    opts.plugins.tooltip.callbacks.label = function (c) { var r = c.raw || {}; return (r.t || "") + "  MD " + (r.x != null ? r.x.toFixed(2) : "?") + "  TIR " + (r.y != null ? r.y.toFixed(2) : "?") + "%"; };
    opts.plugins.legend = { display: false };
    cmpCharts.push(new Chart(document.getElementById("cmp-scatter"), {
      type: "scatter", data: { datasets: bgDatasets.concat(selDatasets) }, options: opts,
      plugins: selBonds.length ? [cmpTickerLabels(selBonds)] : []
    }));
  }

  /* ============================================================
     TAB 3 — DASHBOARD + CUADRANTES
  ============================================================ */
  var DASH_QUADS = [
    { id: "Q1", label: "Corto / Alto rinde", icon: "↗", emoji: "⚡", test: function (b, mMD, mT) { return b.md < mMD && b.tir >= mT; } },
    { id: "Q2", label: "Largo / Alto rinde", icon: "↗", emoji: "🎯", test: function (b, mMD, mT) { return b.md >= mMD && b.tir >= mT; } },
    { id: "Q3", label: "Corto / Bajo rinde", icon: "↙", emoji: "💤", test: function (b, mMD, mT) { return b.md < mMD && b.tir < mT; } },
    { id: "Q4", label: "Largo / Bajo rinde", icon: "↙", emoji: "⚠️", test: function (b, mMD, mT) { return b.md >= mMD && b.tir < mT; } }
  ];
  function dashQuadColor(id) { var t = ON.chartTheme(); return ({ Q1: t.pos, Q2: t.accent, Q3: t.txt, Q4: t.neg })[id] || t.grid; }
  function dashQuadCSS(id) { return ({ Q1: "var(--pos)", Q2: "var(--accent)", Q3: "var(--text-faint)", Q4: "var(--neg)" })[id] || "var(--panel-border)"; }
  function dashAssignQuad(b, mMD, mT) { for (var i = 0; i < DASH_QUADS.length; i++) if (DASH_QUADS[i].test(b, mMD, mT)) return DASH_QUADS[i].id; return null; }

  // dashBase = universo filtrado con MD/TIR válidos (para medianas/scatter/quad-kpis)
  function dashBase() { return filteredList().filter(function (b) { return b.md != null && b.md > 0 && b.tir != null; }); }
  // dashActive = dashBase filtrado por sector activo + cuadrante (para tablas movers/yield + KPIs strip)
  function dashActive() {
    return dashBase().filter(function (b) {
      if (state.dash.activeSector && b.sector !== state.dash.activeSector) return false;
      if (state.dash.quadrant && dashAssignQuad(b, state.dash.medMD, state.dash.medTIR) !== state.dash.quadrant) return false;
      return true;
    });
  }

  var dashQuadPlugin = {
    id: "dash-quadrants",
    afterDraw: function (ch) {
      if (ch.canvas.id !== "dash-quad-canvas") return;
      if (!state.dash.medMD || !state.dash.medTIR) return;
      var ctx = ch.ctx, xS = ch.scales.x, yS = ch.scales.y;
      var xMed = xS.getPixelForValue(state.dash.medMD), yMed = yS.getPixelForValue(state.dash.medTIR);
      var left = ch.chartArea.left, right = ch.chartArea.right, top = ch.chartArea.top, bottom = ch.chartArea.bottom;
      var t = ON.chartTheme();
      ctx.save();
      ctx.setLineDash([6, 4]); ctx.lineWidth = 1.5; ctx.strokeStyle = t.accent; ctx.globalAlpha = 0.55;
      ctx.beginPath(); ctx.moveTo(xMed, top); ctx.lineTo(xMed, bottom); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(left, yMed); ctx.lineTo(right, yMed); ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
      ctx.font = "bold 11px -apple-system, 'Segoe UI', sans-serif"; ctx.textBaseline = "top";
      var labels = [
        { id: "Q1", text: "Corto / Alto rinde", x: left + 8, y: top + 8, tx: "left" },
        { id: "Q2", text: "Largo / Alto rinde", x: right - 8, y: top + 8, tx: "right" },
        { id: "Q3", text: "Corto / Bajo rinde", x: left + 8, y: bottom - 20, tx: "left" },
        { id: "Q4", text: "Largo / Bajo rinde", x: right - 8, y: bottom - 20, tx: "right" }
      ];
      labels.forEach(function (lb) {
        var col = dashQuadColor(lb.id), isActive = state.dash.quadrant === lb.id;
        ctx.textAlign = lb.tx;
        var w = ctx.measureText(lb.text).width, pad = 6;
        var bx = lb.tx === "left" ? lb.x - pad : lb.x - w - pad;
        ctx.fillStyle = isActive ? col : t.grid; ctx.globalAlpha = isActive ? 0.22 : 0.12;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(bx, lb.y - 2, w + pad * 2, 17, 4); else ctx.rect(bx, lb.y - 2, w + pad * 2, 17);
        ctx.fill(); ctx.globalAlpha = 1;
        ctx.fillStyle = col; ctx.globalAlpha = isActive ? 1 : 0.65; ctx.fillText(lb.text, lb.x, lb.y); ctx.globalAlpha = 1;
      });
      ctx.restore();
    }
  };
  if (window.Chart && !window._onQuadReg) { Chart.register(dashQuadPlugin); window._onQuadReg = true; }

  function renderDash() {
    destroyTab("dash");
    var base = dashBase();
    var medMD = ON.median(base.map(function (b) { return b.md; }));
    var medTIR = ON.median(base.map(function (b) { return b.tir; }));
    state.dash.medMD = medMD || 0; state.dash.medTIR = medTIR || 0;
    document.getElementById("dash-med-md").textContent = ON.num(state.dash.medMD, 2) + "a";
    document.getElementById("dash-med-tir").textContent = ON.pct(state.dash.medTIR);
    document.getElementById("dash-scatter-count").textContent = base.length + " bonos con MD/TIR";
    dashKpis(dashActive());
    dashRenderQuadKpis(base);
    dashRenderQBadge();
    dashBuildScatter(base);
    dashRenderBreakdown(dashBase());
    dashRenderMovers();
    dashRenderTopYield();
  }

  function dashKpis(list) {
    var parts = ON.byLey(list), ar = parts.AR || [], ext = parts.EXT || [];
    document.getElementById("dash-kv-n").textContent = list.length;
    document.getElementById("dash-ks-n").textContent = "AR " + ar.length + " / EXT " + ext.length;
    var tirAR = ar.map(function (b) { return b.tir; }).filter(function (v) { return v != null && v > 0; });
    document.getElementById("dash-kv-tir-ar").textContent = ON.avg(tirAR) != null ? ON.pct(ON.avg(tirAR)) : "—";
    document.getElementById("dash-ks-tir-ar").textContent = "mediana " + (tirAR.length ? ON.pct(ON.median(tirAR)) : "—");
    var tirEXT = ext.map(function (b) { return b.tir; }).filter(function (v) { return v != null && v > 0; });
    document.getElementById("dash-kv-tir-ext").textContent = ON.avg(tirEXT) != null ? ON.pct(ON.avg(tirEXT)) : "—";
    document.getElementById("dash-ks-tir-ext").textContent = "mediana " + (tirEXT.length ? ON.pct(ON.median(tirEXT)) : "—");
    var mds = list.map(function (b) { return b.md; }).filter(function (v) { return v != null && v > 0; });
    document.getElementById("dash-kv-md").textContent = ON.avg(mds) != null ? ON.num(ON.avg(mds)) + "a" : "—";
    document.getElementById("dash-ks-md").textContent = mds.length ? "rango " + ON.num(Math.min.apply(null, mds)) + "–" + ON.num(Math.max.apply(null, mds)) + "a" : "rango —";
    var vols = list.map(function (b) { return b.volume || 0; });
    document.getElementById("dash-kv-vol").textContent = ON.vol(ON.sum(vols));
    document.getElementById("dash-ks-vol").textContent = vols.filter(function (v) { return v > 0; }).length + " con operaciones";
    document.getElementById("dash-kv-sec").textContent = ON.bySector(list).size;
    document.getElementById("dash-ks-sec").textContent = "de " + ON.SECTORS.length + " sectores";
  }

  function dashRenderQuadKpis(base) {
    var row = document.getElementById("dash-quad-kpis");
    row.innerHTML = "";
    DASH_QUADS.forEach(function (q) {
      var bonos = base.filter(function (b) { return dashAssignQuad(b, state.dash.medMD, state.dash.medTIR) === q.id; });
      var tirs = bonos.map(function (b) { return b.tir; }), mds = bonos.map(function (b) { return b.md; });
      var div = document.createElement("div");
      div.className = "q-kpi" + (state.dash.quadrant === q.id ? " active" : "");
      div.style.setProperty("--q-color", dashQuadCSS(q.id));
      div.dataset.qid = q.id;
      div.innerHTML = '<div class="q-label">' + q.icon + " " + q.label + '</div><div class="q-icon">' + q.emoji + '</div><div class="q-count">' + bonos.length + '</div>' +
        '<div class="q-stats"><div class="q-stat"><span class="q-stat-v">' + ON.pct(ON.avg(tirs)) + '</span><span class="q-stat-l">TIR prom</span></div><div class="q-stat"><span class="q-stat-v">' + ON.num(ON.avg(mds), 2) + 'a</span><span class="q-stat-l">MD prom</span></div></div>';
      row.appendChild(div);
    });
  }

  function dashRenderQBadge() {
    var el = document.getElementById("dash-q-badge");
    if (!state.dash.quadrant) { el.innerHTML = ""; return; }
    var qm = DASH_QUADS.filter(function (q) { return q.id === state.dash.quadrant; })[0];
    if (!qm) { el.innerHTML = ""; return; }
    el.innerHTML = '<span class="q-badge" style="--q-color:' + dashQuadCSS(state.dash.quadrant) + ';background:' + dashQuadCSS(state.dash.quadrant) + '">' + qm.label + '<span class="q-x" data-clearq="1" title="Quitar filtro">✕</span></span>';
  }

  function dashBuildScatter(base) {
    var sectorMap = new Map();
    base.forEach(function (b) {
      var q = dashAssignQuad(b, state.dash.medMD, state.dash.medTIR), key = b.sector;
      if (!sectorMap.has(key)) sectorMap.set(key, { label: ON.sectorMeta(key).short, color: ON.sectorColor(key), points: [] });
      sectorMap.get(key).points.push({ x: b.md, y: b.tir, t: b.ticker, e: b.emisor, ley: b.ley, paridad: b.paridad, quad: q });
    });
    var datasets = [];
    sectorMap.forEach(function (v) { datasets.push({ label: v.label, data: v.points, backgroundColor: v.color + "cc", borderColor: v.color, borderWidth: 1.2, pointRadius: 6, pointHoverRadius: 9 }); });
    var opts = ON.baseScatterOpts({ xLabel: "Modified Duration (años)", yLabel: "TIR (%)" });
    opts.plugins.tooltip.callbacks.label = function (c) {
      var r = c.raw || {}, qm = DASH_QUADS.filter(function (q) { return q.id === r.quad; })[0];
      return [r.t + " · " + (r.e || ""), "  MD " + (r.x != null ? r.x.toFixed(2) : "?") + "a  ·  TIR " + (r.y != null ? r.y.toFixed(2) : "?") + "%", "  Paridad " + ON.pct(r.paridad) + "  ·  " + (r.ley || ""), qm ? "  ► " + qm.label : ""];
    };
    opts.animation = { duration: 250 };
    opts.onClick = function (evt, elements) {
      if (!elements || !elements.length) return;
      var el = elements[0], pt = dashScatterChart.data.datasets[el.datasetIndex].data[el.index];
      if (pt && pt.quad) dashToggleQuadrant(pt.quad);
    };
    dashScatterChart = trackChart("dash", new Chart(document.getElementById("dash-quad-canvas"), { type: "scatter", data: { datasets: datasets }, options: opts }));
  }
  var dashScatterChart = null;

  function dashRenderBreakdown(list) {
    var container = document.getElementById("dash-breakdown");
    var rows = [];
    ON.bySector(list).forEach(function (bonds, key) {
      rows.push({ key: key, meta: ON.sectorMeta(key), count: bonds.length, vol: ON.sum(bonds.map(function (b) { return b.volume || 0; })), tir: ON.avg(bonds.map(function (b) { return b.tir; }).filter(function (v) { return v != null && v > 0; })) });
    });
    var sortKey = state.dash.bkSort;
    rows.sort(function (a, b) { var va = a[sortKey], vb = b[sortKey]; if (va == null && vb == null) return 0; if (va == null) return 1; if (vb == null) return -1; return vb - va; });
    var maxVal = rows.reduce(function (m, r) { var v = r[sortKey]; return v != null && v > m ? v : m; }, 0);
    container.innerHTML = "";
    rows.forEach(function (r) {
      var val = r[sortKey];
      var barPct = (val != null && maxVal > 0) ? Math.max(2, (val / maxVal) * 100) : 0;
      var label = sortKey === "count" ? r.count + " ONs" : sortKey === "vol" ? ON.vol(r.vol) : (r.tir != null ? ON.pct(r.tir) : "—");
      var row = document.createElement("div");
      row.className = "bk-row" + (state.dash.activeSector === r.key ? " active" : "");
      row.dataset.sector = r.key;
      row.innerHTML = '<div class="bk-name"><span class="bk-dot" style="background:' + r.meta.color + '"></span><span title="' + r.key + '">' + r.meta.short + '</span></div>' +
        '<div style="flex:1;min-width:40px"><div class="bk-bar-wrap"><div class="bk-bar" style="width:' + barPct.toFixed(1) + '%;background:' + r.meta.color + '88"></div></div></div>' +
        '<div class="bk-val">' + label + '</div><div class="bk-tir">' + (r.tir != null ? ON.pct(r.tir, 1) : "—") + '</div>';
      container.appendChild(row);
    });
  }

  function dashRankNum(i) { var cls = i === 0 ? "gold" : i === 1 ? "silver" : i === 2 ? "bronze" : ""; return '<span class="rank-num ' + cls + '">' + (i + 1) + '</span>'; }

  function dashRenderMovers() {
    var list = dashActive(), dir = state.dash.moversDir;
    var sorted = list.slice().filter(function (b) { return b.change_pct != null; }).sort(function (a, b) { return dir === "pos" ? b.change_pct - a.change_pct : a.change_pct - b.change_pct; }).slice(0, 10);
    var tbody = document.getElementById("dash-tbody-movers");
    tbody.innerHTML = sorted.map(function (b, i) {
      var meta = ON.sectorMeta(b.sector);
      return '<tr><td style="text-align:center">' + dashRankNum(i) + '</td><td class="sector-dot"><span class="s-dot" style="background:' + meta.color + '" title="' + ON.esc(meta.short) + '"></span></td>' +
        '<td class="tk" style="text-align:left">' + ON.esc(b.ticker) + '</td><td style="text-align:left;color:var(--text-dim);max-width:120px;overflow:hidden;text-overflow:ellipsis">' + ON.esc(b.emisor) + '</td>' +
        '<td>' + (b.ley === "AR" ? '<span class="ley-badge ar">AR</span>' : '<span class="ley-badge ext">EXT</span>') + '</td><td class="tir num">' + ON.pct(b.tir) + '</td><td class="num">' + ON.pct(b.paridad) + '</td><td class="num ' + ON.sign(b.change_pct) + '">' + ON.pctSigned(b.change_pct) + '</td><td class="num dim">' + ON.vol(b.volume) + '</td></tr>';
    }).join("") || '<tr><td colspan="9" style="text-align:center;padding:24px;color:var(--text-faint)">Sin datos.</td></tr>';
  }

  function dashRenderTopYield() {
    var list = dashActive();
    var sorted = list.slice().filter(function (b) { return b.tir != null && b.tir > 0; }).sort(function (a, b) { return b.tir - a.tir; }).slice(0, 10);
    var tbody = document.getElementById("dash-tbody-yield");
    tbody.innerHTML = sorted.map(function (b, i) {
      var meta = ON.sectorMeta(b.sector);
      return '<tr><td style="text-align:center">' + dashRankNum(i) + '</td><td class="sector-dot"><span class="s-dot" style="background:' + meta.color + '" title="' + ON.esc(meta.short) + '"></span></td>' +
        '<td class="tk" style="text-align:left">' + ON.esc(b.ticker) + '</td><td style="text-align:left;color:var(--text-dim);max-width:120px;overflow:hidden;text-overflow:ellipsis">' + ON.esc(b.emisor) + '</td>' +
        '<td>' + (b.ley === "AR" ? '<span class="ley-badge ar">AR</span>' : '<span class="ley-badge ext">EXT</span>') + '</td><td class="num dim">' + ON.date(b.vto) + '</td><td class="num">' + (b.md != null ? ON.num(b.md) + "a" : "—") + '</td><td class="tir num">' + ON.pct(b.tir) + '</td><td class="num">' + ON.pct(b.paridad, 1) + '</td></tr>';
    }).join("") || '<tr><td colspan="9" style="text-align:center;padding:24px;color:var(--text-faint)">Sin datos.</td></tr>';
  }

  function dashToggleQuadrant(qid) { state.dash.quadrant = (state.dash.quadrant === qid) ? null : qid; renderDash(); }

  /* ============================================================
     WIRING
  ============================================================ */
  function wireSidebar() {
    // colapsar/expandir el panel de facetas (gana ancho para la tabla); persistido
    var sbToggle = document.getElementById("sidebar-toggle");
    var shell = document.querySelector(".on-shell");
    if (sbToggle && shell) {
      try { if (localStorage.getItem("on_sidebar_collapsed") === "1") shell.classList.add("sidebar-collapsed"); } catch (e) {}
      sbToggle.addEventListener("click", function () {
        var collapsed = shell.classList.toggle("sidebar-collapsed");
        try { localStorage.setItem("on_sidebar_collapsed", collapsed ? "1" : "0"); } catch (e) {}
      });
    }
    document.querySelectorAll("input[name='ccy']").forEach(function (el) {
      el.addEventListener("change", function () {
        if (!this.checked) return;
        state.ccy = this.value;
        cardBgCache = {};
        var keep = {}; ON.legs(state.ccy).forEach(function (b) { keep[b.ticker] = true; });
        state.cards.selected = state.cards.selected.filter(function (tk) { return keep[tk]; });
        refresh();
      });
    });
    document.querySelectorAll("input[name='ley']").forEach(function (el) {
      el.addEventListener("change", function () {
        state.ley[this.value] = this.checked;
        if (!Object.keys(state.ley).some(function (k) { return state.ley[k]; })) { state.ley[this.value] = true; this.checked = true; }
        refresh();
      });
    });
    document.querySelectorAll("input[name='tipo']").forEach(function (el) {
      el.addEventListener("change", function () {
        state.tipo[this.value] = this.checked;
        if (!Object.keys(state.tipo).some(function (k) { return state.tipo[k]; })) { state.tipo[this.value] = true; this.checked = true; }
        refresh();
      });
    });
    document.getElementById("fct-sector").addEventListener("change", function (e) {
      if (e.target && e.target.name === "sector") {
        state.sectors[e.target.value] = e.target.checked;
        if (!Object.keys(state.sectors).some(function (k) { return state.sectors[k]; })) { state.sectors[e.target.value] = true; e.target.checked = true; }
        refresh();
      }
    });
    document.querySelectorAll(".clr-link").forEach(function (el) { el.addEventListener("click", function () { clearFacet(this.dataset.clr); }); });
    var timer = null;
    function onRange() {
      clearTimeout(timer);
      timer = setTimeout(function () {
        function g(id) { var v = document.getElementById(id).value; return v !== "" ? v : null; }
        state.vtoMin = g("vto-min") != null ? parseInt(g("vto-min"), 10) : null;
        state.vtoMax = g("vto-max") != null ? parseInt(g("vto-max"), 10) : null;
        state.tirMin = g("tir-min") != null ? parseFloat(g("tir-min")) : null;
        state.tirMax = g("tir-max") != null ? parseFloat(g("tir-max")) : null;
        state.parMin = g("par-min") != null ? parseFloat(g("par-min")) : null;
        state.parMax = g("par-max") != null ? parseFloat(g("par-max")) : null;
        refresh();
      }, 250);
    }
    ["vto-min", "vto-max", "tir-min", "tir-max", "par-min", "par-max"].forEach(function (id) { document.getElementById(id).addEventListener("input", onRange); });
    document.getElementById("reset-btn").addEventListener("click", function () {
      state.ccy = "MEP"; state.ley = { AR: true, EXT: true }; state.tipo = { HD: true, DL: false };
      ON.SECTORS.forEach(function (s) { state.sectors[s.key] = true; }); state.sectors["Otros"] = true;
      state.vtoMin = state.vtoMax = state.tirMin = state.tirMax = state.parMin = state.parMax = null;
      document.querySelectorAll("input[name='ccy']").forEach(function (el) { el.checked = el.value === "MEP"; });
      document.querySelectorAll("input[name='ley']").forEach(function (el) { el.checked = true; });
      document.querySelectorAll("input[name='tipo']").forEach(function (el) { el.checked = el.value === "HD"; });
      document.querySelectorAll("input[name='sector']").forEach(function (el) { el.checked = true; });
      ["vto-min", "vto-max", "tir-min", "tir-max", "par-min", "par-max"].forEach(function (id) { document.getElementById(id).value = ""; });
      cardBgCache = {};
      refresh();
    });
  }

  function wireTabsAndControls() {
    document.getElementById("subtab-bar").addEventListener("click", function (e) {
      var btn = e.target.closest(".subtab"); if (btn) switchTab(btn.dataset.tab);
    });

    // tab 1: tarjetas por sector — orden
    document.getElementById("sec-sort").addEventListener("change", function () { state.sec.sortCards = this.value; secRenderCards(); });

    // tab 1: modal del gráfico TIR-vs-MD (se abre desde el botón "Ver gráfico" de la toolbar)
    var uniBd = document.getElementById("uni-chart-backdrop");
    document.getElementById("uni-chart-close").addEventListener("click", closeUniModal);
    uniBd.addEventListener("click", function (e) { if (e.target === uniBd) closeUniModal(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && uniBd.classList.contains("open")) closeUniModal(); });

    // tab 2: deck controls
    var searchTimer;
    document.getElementById("deck-search").addEventListener("input", function () {
      clearTimeout(searchTimer); var v = this.value;
      searchTimer = setTimeout(function () { state.cards.search = v; cardRenderDeck(); }, 160);
    });
    document.getElementById("deck-sort-key").addEventListener("change", function () { state.cards.sortKey = this.value; cardRenderDeck(); });
    document.getElementById("deck-sort-dir").addEventListener("click", function () { state.cards.sortAsc = !state.cards.sortAsc; this.textContent = state.cards.sortAsc ? "↑" : "↓"; cardRenderDeck(); });
    document.getElementById("deck-grid").addEventListener("click", function (e) {
      var card = e.target.closest(".bond-card"); if (card) cardToggleSelect(card.dataset.tk);
    });
    document.getElementById("cmp-clr").addEventListener("click", function () {
      state.cards.selected = [];
      document.querySelectorAll("#deck-grid .bond-card.selected").forEach(function (c) { c.classList.remove("selected"); });
      cmpRender();
    });
    document.getElementById("cmp-chips-area").addEventListener("click", function (e) {
      var chip = e.target.closest("[data-rm]"); if (!chip) return;
      cardToggleSelect(chip.dataset.rm);
    });

    // tab 3: dashboard controls
    document.getElementById("dash-bk-tabs").addEventListener("click", function (e) {
      var btn = e.target.closest("button"); if (!btn) return;
      state.dash.bkSort = btn.dataset.bk;
      this.querySelectorAll("button").forEach(function (b) { b.classList.toggle("on", b === btn); });
      dashRenderBreakdown(dashBase());
    });
    document.getElementById("dash-seg-dir").addEventListener("click", function (e) {
      var btn = e.target.closest("button"); if (!btn) return;
      state.dash.moversDir = btn.dataset.v;
      this.querySelectorAll("button").forEach(function (b) { b.classList.toggle("on", b === btn); });
      dashRenderMovers();
    });
    document.getElementById("dash-quad-kpis").addEventListener("click", function (e) {
      var box = e.target.closest(".q-kpi"); if (box) dashToggleQuadrant(box.dataset.qid);
    });
    document.getElementById("dash-breakdown").addEventListener("click", function (e) {
      var row = e.target.closest(".bk-row"); if (!row) return;
      state.dash.activeSector = (state.dash.activeSector === row.dataset.sector) ? null : row.dataset.sector;
      document.getElementById("dash-bk-note").textContent = state.dash.activeSector ? ON.sectorMeta(state.dash.activeSector).short : "todos";
      renderDash();
    });
    document.getElementById("dash-q-badge").addEventListener("click", function (e) {
      if (e.target.closest("[data-clearq]")) { state.dash.quadrant = null; renderDash(); }
    });
  }

  // theme change → rebuild active tab
  // tema: base.html togglea data-theme en <html> (sin custom event) → observar y rebuildear
  new MutationObserver(function () {
    if (!ON.DATA.bonds || !ON.DATA.bonds.length) return;
    cardBgCache = {};
    renderKPIs(filteredList());
    renderActiveTab();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  /* ============================================================
     INIT
  ============================================================ */
  function onBoot() { buildSectorFacet(); wireSidebar(); wireTabsAndControls(); refresh(); }
  function onFetch(then) {
    fetch("/on/data").then(function (r) { return r.json(); })
      .then(function (d) { Object.assign(ON.DATA, d); ON.syncSectors(d.sectors_meta); then(); })
      .catch(function () {
        var fn = document.getElementById("footer-note");
        if (fn) fn.textContent = "No se pudo cargar /on/data.";
      });
  }
  onFetch(onBoot);
  // Refresco vivo: re-fetch de /on/data ante cada ciclo del server (precios + ediciones
  // del ABM) y re-render de la pestaña activa; preserva filtros/orden/sectores.
  // `force` saltea el throttle (para el refresco al volver a la pestaña).
  var _onCooldown = false;
  function onLiveRefresh(force) {
    if (_onCooldown && !force) return;
    _onCooldown = true;
    setTimeout(function () { _onCooldown = false; }, 10000);
    onFetch(function () { if (ON.DATA.bonds && ON.DATA.bonds.length) refresh(); });
  }
  try { new EventSource("/stream").addEventListener("refresh", function () { onLiveRefresh(false); }); } catch (e) {}
  setInterval(function () { onLiveRefresh(false); }, 20000);
  // Volver a la pestaña / foco → refrescar YA: si editaste en el ABM y volvés a /on,
  // ves el cambio al instante (sin esperar el próximo ciclo). Dedupe 800ms.
  var _onFocusGuard = false;
  function onFocusRefresh() {
    if (_onFocusGuard) return;
    _onFocusGuard = true;
    setTimeout(function () { _onFocusGuard = false; }, 800);
    onLiveRefresh(true);
  }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) onFocusRefresh(); });
  window.addEventListener("focus", onFocusRefresh);

  /* Guard de visibilidad del refresco vivo (solo aplica en la página /on).
     El loop vivo (SSE + polling de /on/data) NO está en este mock: lo inyecta
     scripts/build_on_static.py al portearlo a static/js/on.js. Por eso el guard vive
     acá y no allá: on.js es un artefacto generado y cualquier edición a mano se pierde
     en el próximo build. Con la pestaña oculta no re-fetcheamos ni re-renderizamos
     (nadie lo mira; mismo criterio que mrRefreshOK en base.html) — al volver, el
     visibilitychange del loop llama con force=true y re-sincroniza al instante.
     En el mock standalone onLiveRefresh no existe y esto queda en no-op. */
  if (typeof onLiveRefresh === "function") {
    var _onLiveRefreshRaw = onLiveRefresh;
    onLiveRefresh = function (force) { if (document.hidden) return; _onLiveRefreshRaw(force); };
  }

})();
